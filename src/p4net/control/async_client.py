"""Asynchronous P4Runtime client using ``grpc.aio``.

Connection setup, writes, counters, registers, and packet I/O use
coroutines. ``list_table_entries`` returns an async iterator. Use and
close each client's gRPC resources on the event loop where it connected.

Sync and async clients have separate channels and election IDs. Coordinate
primary status explicitly when connecting both to the same device.

The public API is stable in p4net 1.x since version 1.7.0. See
``docs/api-stability.md`` for the contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, NamedTuple

import grpc
from google.protobuf import text_format
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

from p4net.control._rpc_errors import _extract_p4_canonical_codes, _grpc_code_for
from p4net.control.codec import encode_value
from p4net.control.exceptions import (
    AsyncOperationCancelledError,
    ConnectionError,
    DuplicateEntryError,
    EncodingError,
    EntryNotFoundError,
    NotPrimaryError,
    P4RuntimeError,
    PipelineError,
)
from p4net.control.p4info_index import P4InfoIndex, _index_after_commit

logger = logging.getLogger(__name__)

_PIPELINE_ACTIONS: frozenset[str] = frozenset(
    {"VERIFY", "VERIFY_AND_SAVE", "VERIFY_AND_COMMIT", "COMMIT", "RECONCILE_AND_COMMIT"}
)
_PACKET_IN_QUEUE_CAPACITY = 256
_PacketIn = tuple[bytes, dict[str, int]]
_PACKET_IN_CALLBACK: ContextVar[tuple[AsyncP4RuntimeClient, Any] | None] = ContextVar(
    "p4net_packet_in_callback", default=None
)


class _PacketInClosed(NamedTuple):
    cause: BaseException | None


def _parse_register_read_single(output: str, name: str, index: int) -> int:
    needle = f"{name}["
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("RuntimeCmd:"):
            line = line[len("RuntimeCmd:") :].strip()
        if line.startswith(needle):
            eq = line.find("=")
            if eq == -1:
                continue
            try:
                return int(line[eq + 1 :].strip())
            except ValueError as exc:
                raise P4RuntimeError(
                    f"register_read {name}[{index}]: could not parse value"
                ) from exc
    raise P4RuntimeError(f"register_read {name}[{index}]: no value line in output")


def _parse_register_read_array(output: str, name: str, size: int) -> list[int]:
    needle = f"{name}="
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("RuntimeCmd:"):
            line = line[len("RuntimeCmd:") :].strip()
        if line.startswith(needle):
            value_text = line[len(needle) :].strip()
            try:
                values = [int(v.strip()) for v in value_text.split(",") if v.strip()]
            except ValueError as exc:
                raise P4RuntimeError(f"register_read {name}: could not parse values") from exc
            if len(values) != size:
                raise P4RuntimeError(
                    f"register_read {name}: expected {size} cells, got {len(values)}"
                )
            return values
    raise P4RuntimeError(f"register_read {name}: no value line in output")


class AsyncP4RuntimeClient:
    """Async parallel to :class:`P4RuntimeClient`. **Stable** in 1.x since 1.7.0."""

    def __init__(
        self,
        grpc_address: tuple[str, int],
        device_id: int,
        info_index: P4InfoIndex | None = None,
        thrift_address: tuple[str, int] | None = None,
        election_id: tuple[int, int] | None = None,
    ) -> None:
        self._host, self._port = grpc_address
        self._device_id = int(device_id)
        self._info_index = info_index
        self._thrift_address = thrift_address
        self._election_id = election_id  # resolved at connect() if None

        self._channel: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stub: Any = None
        self._outgoing: asyncio.Queue[Any] | None = None
        self._stream_call: Any = None
        self._stream_task: asyncio.Task[None] | None = None
        self._connect_attempt: asyncio.Future[BaseException | None] | None = None
        self._arbitration_event: asyncio.Event | None = None
        self._arbitration: Any = None
        self._stream_error: BaseException | None = None
        self._packet_in_queue: asyncio.Queue[_PacketIn | _PacketInClosed] | None = None
        self._packet_handlers: list[Callable[[bytes, dict[str, int]], Awaitable[None]]] = []
        self._handlers_lock = asyncio.Lock()
        self._teardown_lock = asyncio.Lock()
        self._pipeline_lock = asyncio.Lock()
        self._connected = False
        self._is_primary = False
        self._closed = False

    # ----------------------------------------------------------------- properties

    @property
    def grpc_address(self) -> tuple[str, int]:
        return (self._host, self._port)

    @property
    def target(self) -> str:
        return f"{self._host}:{self._port}"

    @property
    def device_id(self) -> int:
        return self._device_id

    @property
    def election_id(self) -> tuple[int, int]:
        if self._election_id is None:
            raise P4RuntimeError("election_id not yet assigned (connect() pending)")
        return self._election_id

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._closed

    @property
    def is_primary(self) -> bool:
        """Whether the latest arbitration response grants primary status."""
        return self._is_primary

    @property
    def info_index(self) -> P4InfoIndex:
        if self._info_index is None:
            raise P4RuntimeError(
                "no P4Info index attached; push a pipeline first or pass info_index= to ctor"
            )
        return self._info_index

    # ----------------------------------------------------------------- lifecycle

    async def connect(self, *, timeout: float = 10.0) -> None:
        """Connect and await arbitration. Concurrent callers share the first attempt."""
        if self._connected:
            return
        attempt = self._connect_attempt
        if attempt is not None:
            failure = await asyncio.shield(attempt)
            if failure is not None:
                raise failure
            return

        attempt = asyncio.get_running_loop().create_future()
        self._connect_attempt = attempt
        try:
            await self._connect(timeout=timeout)
        except BaseException as exc:
            try:
                await self._teardown()
            finally:
                attempt.set_result(exc)
            raise
        else:
            attempt.set_result(None)
        finally:
            self._connect_attempt = None

    async def _connect(self, *, timeout: float) -> None:
        if self._channel is not None:
            await self._teardown()
        self._closed = False
        self._loop = asyncio.get_running_loop()
        self._channel = grpc.aio.insecure_channel(self.target)
        self._stub = p4runtime_pb2_grpc.P4RuntimeStub(self._channel)
        outgoing = self._outgoing = asyncio.Queue()
        self._arbitration_event = asyncio.Event()
        self._arbitration = None
        self._stream_error = None
        self._packet_in_queue = asyncio.Queue(maxsize=_PACKET_IN_QUEUE_CAPACITY)

        if self._election_id is None:
            millis = time.time_ns() // 1_000_000
            self._election_id = (int(millis), 0)

        requests = self._request_generator(outgoing)
        stream_call = self._stream_call = self._stub.StreamChannel(requests)
        self._stream_task = asyncio.create_task(
            self._stream_consumer(stream_call), name=f"p4rt-async-stream-{self._host}:{self._port}"
        )

        req = p4runtime_pb2.StreamMessageRequest()
        req.arbitration.device_id = self._device_id
        req.arbitration.election_id.high = self._election_id[0]
        req.arbitration.election_id.low = self._election_id[1]
        await outgoing.put(req)

        try:
            await asyncio.wait_for(self._arbitration_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ConnectionError(
                f"P4Runtime async arbitration timed out after {timeout}s for {self.target!r}"
            ) from exc
        if self._stream_error is not None:
            err = self._stream_error
            if isinstance(err, ConnectionError):
                raise err
            raise P4RuntimeError(f"async stream error: {err!r}") from err
        if self._arbitration is None:
            raise ConnectionError("no arbitration response received")
        status_code = int(self._arbitration.status.code)
        if status_code != 0 and self._election_id != (0, 0):
            # Explicit secondary (election_id=(0,0)) is allowed and stays connected.
            # Any other non-primary status is a failure.
            raise NotPrimaryError(
                f"async client is not primary for device {self._device_id} "
                f"(status code {status_code})"
            )
        self._connected = True
        logger.info(
            "AsyncP4RuntimeClient %s connected (primary=%s, election=%s)",
            self.target,
            self._is_primary,
            self._election_id,
        )

    async def disconnect(self) -> None:
        """Close the channel and release connection resources.

        In a PacketIn callback, close the stream that delivered the packet.
        A cancellation during channel closure is propagated after cleanup.
        """
        callback = _PACKET_IN_CALLBACK.get()
        if callback is not None and callback[0] is self:
            await self._teardown(expected_stream=callback[1], from_packet_handler=True)
        else:
            await self._teardown(expected_stream=self._stream_call)

    async def _teardown(
        self, *, expected_stream: Any = None, from_packet_handler: bool = False
    ) -> None:
        caller = self._stream_task if from_packet_handler else asyncio.current_task()
        async with self._teardown_lock:
            if expected_stream is not None and expected_stream is not self._stream_call:
                return
            self._closed = True
            was_connected = self._connected
            self._connected = False
            self._is_primary = False
            if self._channel is not None and not was_connected:
                if self._stream_error is None:
                    self._stream_error = ConnectionError(
                        f"P4Runtime async connection closed before arbitration for {self.target!r}"
                    )
                if self._arbitration_event is not None:
                    self._arbitration_event.set()
            queue = self._packet_in_queue
            self._packet_in_queue = None
            if queue is not None:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(_PacketInClosed(self._stream_error))
            if self._outgoing is not None:
                with self._suppress_queue_full():
                    self._outgoing.put_nowait(None)  # sentinel
            if self._stream_call is not None:
                try:
                    self._stream_call.cancel()
                except Exception as exc:
                    logger.debug("async stream cancel raised: %r", exc)
            stream_task = self._stream_task
            if stream_task is not None and stream_task is not caller and not stream_task.done():
                stream_task.cancel()
            else:
                stream_task = None
            cancelled = await self._close_channel()
            self._channel = None
            self._loop = None
            self._stub = None
            self._stream_call = None
            self._stream_task = None
            self._outgoing = None
            if was_connected:
                logger.info("AsyncP4RuntimeClient %s disconnected", self.target)

        if cancelled:
            raise asyncio.CancelledError
        # A callback may call disconnect from its cancellation handler.
        # Wait for it only after releasing the lifecycle lock.
        if stream_task is not None:
            done, _ = await asyncio.wait((stream_task,), timeout=2.0)
            if done and not stream_task.cancelled():
                error = stream_task.exception()
                if error is not None:
                    logger.debug("async stream task awaited with: %r", error)

    async def _close_channel(self) -> bool:
        """Finish closing the channel and report whether the caller was cancelled."""
        if self._channel is None:
            return False
        cancelled = False
        try:
            closing = asyncio.create_task(self._channel.close())
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    cancelled = True
            closing.result()
        except Exception as exc:
            logger.debug("async channel close raised: %r", exc)
        return cancelled

    @staticmethod
    def _suppress_queue_full() -> Any:
        return contextlib.suppress(asyncio.QueueFull)

    async def __aenter__(self) -> AsyncP4RuntimeClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.disconnect()

    # ----------------------------------------------------------------- stream

    async def _request_generator(self, outgoing: asyncio.Queue[Any]) -> AsyncIterator[Any]:
        while True:
            msg = await outgoing.get()
            if msg is None:
                return
            yield msg

    def _stream_is_current(self, stream_call: Any) -> bool:
        return not self._closed and stream_call is self._stream_call

    async def _stream_consumer(self, stream_call: Any) -> None:
        error: Exception | None = None
        try:
            async for resp in stream_call:
                if not self._stream_is_current(stream_call):
                    return
                if resp.HasField("arbitration"):
                    self._arbitration = resp.arbitration
                    self._is_primary = int(resp.arbitration.status.code) == 0
                    assert self._arbitration_event is not None
                    self._arbitration_event.set()
                elif resp.HasField("packet"):
                    await self._dispatch_packet_in(resp.packet, stream_call)
        except Exception as exc:
            error = exc
        finally:
            if self._stream_is_current(stream_call):
                if error is None:
                    error = ConnectionError(f"P4Runtime async stream ended for {self.target!r}")
                self._stream_error = error
                if self._arbitration_event is not None:
                    self._arbitration_event.set()
                await self._teardown(expected_stream=stream_call)

    async def _dispatch_packet_in(self, packet: Any, stream_call: Any) -> None:
        payload = bytes(packet.payload)
        meta: dict[str, int] = {}
        if self._info_index is not None:
            try:
                meta = self._info_index.decode_packet_in_metadata(packet.metadata)
            except Exception as exc:
                logger.debug("async decode_packet_in_metadata raised: %r", exc)
        # Buffer independently of callback delivery.
        if self._packet_in_queue is not None:
            with self._suppress_queue_full():
                self._packet_in_queue.put_nowait((payload, meta))
        # Dispatch to async handlers.
        async with self._handlers_lock:
            handlers = list(self._packet_handlers)
        token = _PACKET_IN_CALLBACK.set((self, stream_call))
        try:
            for h in handlers:
                if not self._stream_is_current(stream_call):
                    return
                try:
                    await h(payload, meta)
                except Exception as exc:
                    logger.warning("async packet_in handler %r raised: %r", h, exc)
        finally:
            _PACKET_IN_CALLBACK.reset(token)

    # ----------------------------------------------------------------- pipeline

    async def push_pipeline(
        self,
        p4info_bytes: bytes | None = None,
        json_bytes: bytes | None = None,
        *,
        action: str = "VERIFY_AND_COMMIT",
        timeout: float = 10.0,
    ) -> None:
        """Push a compiled pipeline (raw bytes).

        ``VERIFY`` validates the candidate and retains the current P4Info index.
        ``COMMIT`` uses the configuration already saved on the switch and reads
        back its P4Info. The bytes arguments are only used for other actions.
        The timeout applies to each RPC.

        The client raises ``ConnectionError`` if the connection closes while
        it is waiting for a reply or before it can apply the reply.
        """
        self._require_connected()
        stub = self._stub
        if action not in _PIPELINE_ACTIONS:
            raise P4RuntimeError(f"invalid pipeline action {action!r}")
        req = p4runtime_pb2.SetForwardingPipelineConfigRequest()
        req.device_id = self._device_id
        req.election_id.high = self._election_id[0]  # type: ignore[index]
        req.election_id.low = self._election_id[1]  # type: ignore[index]
        req.action = p4runtime_pb2.SetForwardingPipelineConfigRequest.Action.Value(action)
        msg = None
        if action != "COMMIT":
            if p4info_bytes is None or json_bytes is None:
                raise TypeError(f"p4info_bytes and json_bytes are required for {action}")
            msg = p4info_pb2.P4Info()
            text_format.Merge(p4info_bytes.decode("utf-8"), msg)
            req.config.p4info.CopyFrom(msg)
            req.config.p4_device_config = bytes(json_bytes)

        try:
            async with self._pipeline_lock:
                if self._stub is not stub or not self.is_connected:
                    raise ConnectionError(
                        f"Connection to {self.target!r} closed before pipeline RPC"
                    )
                await self._await_rpc(
                    stub,
                    lambda: stub.SetForwardingPipelineConfig(req),
                    connection_closed=f"Connection to {self.target!r} closed during pipeline RPC",
                    timeout=timeout,
                )
                if msg is None:
                    await self._refresh_committed_pipeline(stub, timeout=timeout)
                    return
                if self._stub is not stub or not self.is_connected:
                    raise ConnectionError(
                        f"Connection to {self.target!r} closed before applying pipeline reply"
                    )
                if action != "VERIFY":
                    self._info_index = P4InfoIndex(msg)
        except asyncio.CancelledError as exc:
            raise AsyncOperationCancelledError("push_pipeline cancelled") from exc
        except grpc.aio.AioRpcError as exc:
            raise self._translate_rpc_error(exc, pipeline=True) from exc

    async def _refresh_committed_pipeline(self, stub: Any, *, timeout: float) -> None:
        connection_closed = "COMMIT succeeded. Connection closed during P4Info refresh."
        if self._stub is not stub or not self.is_connected:
            raise ConnectionError(connection_closed)
        previous, self._info_index = self._info_index, None
        req = p4runtime_pb2.GetForwardingPipelineConfigRequest()
        req.device_id = self._device_id
        req.response_type = p4runtime_pb2.GetForwardingPipelineConfigRequest.P4INFO_AND_COOKIE
        try:
            response = await self._await_rpc(
                stub,
                lambda: stub.GetForwardingPipelineConfig(req),
                connection_closed=connection_closed,
                timeout=timeout,
            )
        except asyncio.CancelledError as exc:
            raise AsyncOperationCancelledError(
                "COMMIT succeeded. P4Info refresh cancelled."
            ) from exc
        except asyncio.TimeoutError as exc:
            raise asyncio.TimeoutError("COMMIT succeeded. P4Info refresh timed out.") from exc
        except grpc.aio.AioRpcError as exc:
            error = self._translate_rpc_error(exc)
            raise type(error)(f"COMMIT succeeded. P4Info refresh failed: {error}") from exc
        if self._stub is not stub or not self.is_connected:
            raise ConnectionError(connection_closed)
        self._info_index = _index_after_commit(response.config, previous)

    # ----------------------------------------------------------------- tables

    async def insert_table_entry(
        self,
        table: str,
        match: Mapping[str, object],
        action: str,
        params: Mapping[str, object] | None = None,
        *,
        priority: int | None = None,
        timeout: float = 5.0,
    ) -> None:
        """Insert an entry with an int priority in [1, 2147483647], excluding bool.

        Optional, ternary and range tables require ``priority``, including
        wildcard entries. Omit it for exact/LPM-only tables.
        """
        await self._write_entry(table, match, action, params, priority, "INSERT", timeout)

    async def modify_table_entry(
        self,
        table: str,
        match: Mapping[str, object],
        action: str,
        params: Mapping[str, object] | None = None,
        *,
        priority: int | None = None,
        timeout: float = 5.0,
    ) -> None:
        """Modify an entry using the priority rules of :meth:`insert_table_entry`."""
        await self._write_entry(table, match, action, params, priority, "MODIFY", timeout)

    async def delete_table_entry(
        self,
        table: str,
        match: Mapping[str, object],
        *,
        priority: int | None = None,
        timeout: float = 5.0,
    ) -> None:
        """Delete by match key and original priority.

        Optional, ternary and range tables require ``priority``, including
        wildcard entries. Use an int in [1, 2147483647], excluding bool.
        Omit it for exact/LPM-only tables.
        """
        await self._write_entry(table, match, None, None, priority, "DELETE", timeout)

    async def _write_entry(
        self,
        table: str,
        match: Mapping[str, object],
        action: str | None,
        params: Mapping[str, object] | None,
        priority: int | None,
        update_type: str,
        timeout: float,
    ) -> None:
        self._require_connected_with_index()
        stub, idx = self._stub, self.info_index
        table_id = idx.table_id(table)
        requires_priority = idx.table_requires_priority(table)
        if requires_priority and priority is None:
            raise EncodingError(
                f"table {table!r} has optional/ternary/range fields; priority required"
            )
        if not requires_priority and priority is not None:
            raise EncodingError(f"table {table!r} is exact/lpm-only; priority must be None")
        if priority is not None and (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or not 1 <= priority <= 0x7FFFFFFF
        ):
            raise EncodingError("priority must be an int in [1, 2147483647] (bool is not accepted)")
        fms = idx.encode_match(table, match)
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = table_id
        for fm in fms:
            entry.match.add().CopyFrom(fm)
        if action is not None:
            entry.action.action.CopyFrom(idx.encode_action(action, params))
        if priority is not None:
            entry.priority = priority
        upd = p4runtime_pb2.Update()
        upd.type = p4runtime_pb2.Update.Type.Value(update_type)
        upd.entity.table_entry.CopyFrom(entry)
        req = p4runtime_pb2.WriteRequest()
        req.device_id = self._device_id
        req.election_id.high = self._election_id[0]  # type: ignore[index]
        req.election_id.low = self._election_id[1]  # type: ignore[index]
        req.updates.add().CopyFrom(upd)

        async def write() -> Any:
            self._require_same_indexed_context(stub, idx, operation="table write")
            return await stub.Write(req)

        try:
            await self._await_rpc(
                stub,
                write,
                connection_closed=f"Connection to {self.target!r} closed during a table write",
                timeout=timeout,
            )
        except asyncio.CancelledError as exc:
            raise AsyncOperationCancelledError(f"{update_type} on {table!r} cancelled") from exc
        except grpc.aio.AioRpcError as exc:
            raise self._translate_rpc_error(exc) from exc

    def list_table_entries(
        self,
        table: str | None = None,
        *,
        timeout: float = 5.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over decoded table entries.

        Use it as ``async for entry in client.list_table_entries(...)``.
        The RPC starts when iteration begins. The iterator becomes invalid
        if its connection closes or its P4Info changes, even after iteration
        has started. A closed connection raises ``ConnectionError``. Create
        a new iterator to read again.
        """
        self._require_connected_with_index()
        stub = self._stub
        idx = self.info_index
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self._device_id
        entity = req.entities.add()
        if table is not None:
            entity.table_entry.table_id = idx.table_id(table)
        else:
            entity.table_entry.table_id = 0

        def require_current_context(when: str) -> None:
            if not self.is_connected or self._stub is not stub:
                raise ConnectionError(
                    f"Connection to {self.target!r} closed {when} reading table entries"
                )
            if self._info_index is not idx:
                raise P4RuntimeError(
                    f"P4Info changed {when} reading table entries. Create a new iterator."
                )

        async def _gen() -> AsyncIterator[dict[str, Any]]:
            require_current_context("before")
            async with contextlib.aclosing(
                self._read_responses(
                    stub, req, timeout=timeout, operation="while reading table entries"
                )
            ) as responses:
                async for resp in responses:
                    require_current_context("while")
                    for ent in resp.entities:
                        if ent.HasField("table_entry"):
                            yield self._decode_table_entry(ent.table_entry, idx)
                            require_current_context("while")
            require_current_context("while")

        return _gen()

    async def _await_rpc(
        self,
        stub: Any,
        start: Callable[[], Awaitable[Any]],
        *,
        connection_closed: str,
        timeout: float | None = None,
    ) -> Any:
        def require_connection(cause: BaseException | None = None) -> None:
            if self._stub is not stub or not self.is_connected:
                raise ConnectionError(connection_closed) from cause

        async def invoke() -> Any:
            require_connection()
            try:
                return await start()
            except (asyncio.CancelledError, grpc.aio.AioRpcError) as exc:
                if (
                    isinstance(exc, asyncio.CancelledError)
                    or exc.code() == grpc.StatusCode.CANCELLED
                ):
                    require_connection(exc)
                raise

        if timeout is not None and timeout <= 0:
            raise asyncio.TimeoutError
        task = asyncio.create_task(invoke())
        try:
            # Waiting for completion separates a caller cancellation from
            # cancellation reported by the RPC itself.
            done, _ = await asyncio.wait((task,), timeout=timeout)
            if not done:
                raise asyncio.TimeoutError
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        return task.result()

    async def _read_responses(
        self, stub: Any, request: Any, *, timeout: float, operation: str
    ) -> AsyncGenerator[Any, None]:
        connection_closed = f"Connection to {self.target!r} closed {operation}"
        call = None
        try:
            call = stub.Read(request, timeout=timeout)
            responses = aiter(call)
            while True:
                try:
                    response = await self._await_rpc(
                        stub, lambda: anext(responses), connection_closed=connection_closed
                    )
                except StopAsyncIteration:
                    break
                yield response
        except grpc.aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.CANCELLED and (
                self._stub is not stub or not self.is_connected
            ):
                raise ConnectionError(connection_closed) from exc
            raise self._translate_rpc_error(exc) from exc
        finally:
            if isinstance(call, grpc.aio.Call) and not call.done():
                call.cancel()

    @staticmethod
    def _decode_table_entry(entry: Any, idx: P4InfoIndex) -> dict[str, Any]:
        table_name = idx.table_name(int(entry.table_id))
        table = None
        for t in idx.raw.tables:
            if t.preamble.id == entry.table_id:
                table = t
                break
        assert table is not None
        fields_by_id = {int(mf.id): mf for mf in table.match_fields}
        match: dict[str, Any] = {}
        for fm in entry.match:
            mf = fields_by_id.get(int(fm.field_id))
            if mf is None:
                continue
            if fm.HasField("exact"):
                match[mf.name] = bytes(fm.exact.value)
            elif fm.HasField("lpm"):
                match[mf.name] = (bytes(fm.lpm.value), int(fm.lpm.prefix_len))
            elif fm.HasField("ternary"):
                match[mf.name] = (bytes(fm.ternary.value), bytes(fm.ternary.mask))
            elif fm.HasField("range"):
                match[mf.name] = (bytes(fm.range.low), bytes(fm.range.high))
            elif fm.HasField("optional"):
                match[mf.name] = bytes(fm.optional.value)
        action_name: str | None = None
        params: dict[str, bytes] = {}
        if entry.HasField("action") and entry.action.HasField("action"):
            a = entry.action.action
            action_name = idx.action_name(int(a.action_id))
            for am in idx.raw.actions:
                if am.preamble.id == a.action_id:
                    params_by_id = {int(p.id): p for p in am.params}
                    for ap in a.params:
                        p = params_by_id.get(int(ap.param_id))
                        if p is not None:
                            params[p.name] = bytes(ap.value)
                    break
        return {
            "table": table_name,
            "match": match,
            "action": action_name,
            "params": params,
            "priority": int(entry.priority) if entry.priority else None,
        }

    # ----------------------------------------------------------------- counters

    async def read_counter(
        self,
        counter: str,
        index: int | None = None,
        *,
        timeout: float = 5.0,
    ) -> int | dict[int, int]:
        """Read packet counts from an indirect counter.

        A single index returns the cell's packet count, with zero for an
        absent cell. Omitting the index returns ``{cell_index: packet_count}``.
        The read uses the connection and P4Info selected at the start of the call.
        A closed connection raises ``ConnectionError``.
        """
        self._require_connected_with_index()
        stub, idx = self._stub, self.info_index
        counter_id = idx.counter_id(counter)
        cell = int(index) if index is not None else None
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self._device_id
        entity = req.entities.add()
        entity.counter_entry.counter_id = counter_id
        if cell is not None:
            entity.counter_entry.index.index = cell
        collected: dict[int, int] = {}
        self._require_same_indexed_context(stub, idx, operation="counter read")
        try:
            async with contextlib.aclosing(
                self._read_responses(stub, req, timeout=timeout, operation="during a counter read")
            ) as responses:
                async for resp in responses:
                    self._require_same_indexed_context(stub, idx, operation="counter read")
                    for ent in resp.entities:
                        if not ent.HasField("counter_entry"):
                            continue
                        ce = ent.counter_entry
                        collected[int(ce.index.index)] = int(ce.data.packet_count)
            self._require_same_indexed_context(stub, idx, operation="counter read")
        except asyncio.CancelledError as exc:
            raise AsyncOperationCancelledError("read_counter cancelled") from exc
        if cell is not None:
            return collected.get(cell, 0)
        return collected

    # ----------------------------------------------------------------- registers

    async def write_register(
        self,
        name: str,
        index: int,
        value: int,
        *,
        timeout: float = 5.0,
    ) -> None:
        """Write a register cell through BMv2's Thrift service.

        The connection and P4Info must still match when the Thrift command starts.
        """
        self._require_connected_with_index()
        stub, idx = self._stub, self.info_index
        spec = idx.register_by_name(name)
        if not 0 <= int(index) < spec.size:
            raise EncodingError(f"register {name!r} index {index} out of range [0, {spec.size})")
        encode_value(int(value), spec.bitwidth)  # pre-flight bitwidth check
        await self._run_register_command(
            stub,
            idx,
            f"register_write {name} {int(index)} {int(value)}",
            timeout=timeout,
            op_description=f"register_write {name}[{index}]",
        )

    async def read_register(
        self,
        name: str,
        index: int | None = None,
        *,
        timeout: float = 5.0,
    ) -> int | list[int]:
        """Read a register cell or its entire array through BMv2's Thrift service.

        The connection and P4Info must still match when the Thrift command starts.
        """
        self._require_connected_with_index()
        stub, idx = self._stub, self.info_index
        spec = idx.register_by_name(name)
        if index is not None and not 0 <= int(index) < spec.size:
            raise EncodingError(f"register {name!r} index {index} out of range [0, {spec.size})")
        if index is not None:
            output = await self._run_register_command(
                stub,
                idx,
                f"register_read {name} {int(index)}",
                timeout=timeout,
                op_description=f"register_read {name}[{index}]",
            )
            return _parse_register_read_single(output, name, int(index))
        output = await self._run_register_command(
            stub,
            idx,
            f"register_read {name}",
            timeout=timeout,
            op_description=f"register_read {name}",
        )
        return _parse_register_read_array(output, name, spec.size)

    async def _run_register_command(
        self,
        stub: Any,
        index: P4InfoIndex,
        command: str,
        *,
        timeout: float,
        op_description: str,
    ) -> str:
        try:
            # The CLI opens a separate connection. Keep the client and its
            # pipeline stable until the command has finished or been stopped.
            async with self._pipeline_lock, self._teardown_lock:
                self._require_same_indexed_context(stub, index, operation="register operation")
                return await self._run_thrift_cli(
                    command, timeout=timeout, op_description=op_description
                )
        except asyncio.CancelledError as exc:
            raise AsyncOperationCancelledError(f"{op_description} cancelled") from exc

    async def _run_thrift_cli(
        self,
        command: str,
        *,
        timeout: float,
        op_description: str,
    ) -> str:
        if self._thrift_address is None:
            raise P4RuntimeError(
                f"register operations require thrift_address=(host, port); op: {op_description}"
            )
        host, port = self._thrift_address
        if shutil.which("simple_switch_CLI") is None:
            raise P4RuntimeError("simple_switch_CLI not on PATH; required for register ops")
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                "simple_switch_CLI",
                "--thrift-ip",
                str(host),
                "--thrift-port",
                str(int(port)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
        cancelled_on_launch = await self._wait_for_thrift_task(launch)
        try:
            proc = launch.result()
        except FileNotFoundError as exc:
            if cancelled_on_launch:
                raise asyncio.CancelledError from exc
            raise P4RuntimeError("simple_switch_CLI not found") from exc

        timed_out = timeout is not None and timeout <= 0
        command_input = (
            b"" if cancelled_on_launch or timed_out else (command + "\n").encode("utf-8")
        )
        communication = asyncio.create_task(proc.communicate(input=command_input))
        try:
            if cancelled_on_launch:
                raise asyncio.CancelledError
            if timed_out:
                raise asyncio.TimeoutError
            done, _ = await asyncio.wait((communication,), timeout=timeout)
            if not done:
                raise asyncio.TimeoutError
            stdout, stderr = communication.result()
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            # Draining the pipes lets communicate reap the process even when
            # stdout or stderr is full. Further cancellations must wait for it.
            cancelled_while_stopping = await self._wait_for_thrift_task(communication)
            if not communication.cancelled():
                communication.exception()
            if isinstance(exc, asyncio.CancelledError) or cancelled_while_stopping:
                raise asyncio.CancelledError from exc
            raise P4RuntimeError(f"{op_description} timed out after {timeout}s") from exc
        if proc.returncode != 0:
            raise P4RuntimeError(
                f"{op_description} failed (rc={proc.returncode}): "
                f"{(stdout + stderr).decode('utf-8', errors='replace').strip()}"
            )
        combined = (stdout + stderr).decode("utf-8", errors="replace")
        for line in combined.splitlines():
            stripped = line.strip()
            if stripped.startswith("Error:") or "Invalid" in stripped:
                raise P4RuntimeError(f"{op_description}: {stripped}")
        return stdout.decode("utf-8", errors="replace")

    @staticmethod
    async def _wait_for_thrift_task(task: asyncio.Task[Any]) -> bool:
        """Wait for subprocess launch or cleanup and report caller cancellation."""
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                cancelled = True
        return cancelled

    # ----------------------------------------------------------------- packets

    async def send_packet_out(
        self,
        payload: bytes,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self._require_connected()
        idx = self._info_index
        encoded = idx.encode_packet_out_metadata(metadata or {}) if idx is not None else []
        if metadata and idx is None:
            raise EncodingError("no pipeline set; cannot encode packet_out metadata")
        if not isinstance(payload, (bytes, bytearray)):
            raise EncodingError(f"payload must be bytes-like, got {type(payload).__name__}")
        req = p4runtime_pb2.StreamMessageRequest()
        req.packet.payload = bytes(payload)
        for pm in encoded:
            req.packet.metadata.add().CopyFrom(pm)
        out = self._outgoing
        if out is None:
            raise ConnectionError("client is not connected")
        await out.put(req)

    async def on_packet_in(
        self,
        handler: Callable[[bytes, dict[str, int]], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]:
        """Register an async PacketIn handler. Returns an async unsubscribe."""
        async with self._handlers_lock:
            self._packet_handlers.append(handler)

        async def deregister() -> None:
            async with self._handlers_lock:
                with contextlib.suppress(ValueError):
                    self._packet_handlers.remove(handler)

        return deregister

    async def expect_packet_in(
        self,
        *,
        timeout: float = 5.0,
    ) -> tuple[bytes, dict[str, int]]:
        """Return the oldest queued PacketIn or wait when the queue is empty.

        The queue holds up to 256 packets per connection. When it is full,
        new packets go only to registered handlers.

        Raises ConnectionError if the connection closes during the wait,
        or P4RuntimeError on timeout.
        """
        self._require_connected()
        queue = self._packet_in_queue
        if queue is None:
            raise ConnectionError("packet_in queue is not initialised")

        async def receive() -> tuple[bytes, dict[str, int]]:
            packet = await queue.get()
            if isinstance(packet, _PacketInClosed):
                # Pass the close marker to the next reader before raising.
                queue.put_nowait(packet)
                raise ConnectionError(
                    f"Connection to {self.target!r} closed while waiting for PacketIn"
                ) from packet.cause
            return packet

        try:
            return await asyncio.wait_for(receive(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise P4RuntimeError(f"no PacketIn within {timeout}s on {self.target!r}") from exc
        except asyncio.CancelledError as exc:
            raise AsyncOperationCancelledError("expect_packet_in cancelled") from exc

    # ----------------------------------------------------------------- internals

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError(f"AsyncP4RuntimeClient {self.target} is not connected")

    def _require_connected_with_index(self) -> None:
        self._require_connected()
        if self._info_index is None:
            raise P4RuntimeError(
                "no P4Info index; pass info_index= to ctor or call push_pipeline first"
            )

    def _require_same_indexed_context(
        self, stub: Any, index: P4InfoIndex, *, operation: str
    ) -> None:
        if self._stub is not stub or not self.is_connected:
            raise ConnectionError(f"Connection to {self.target!r} closed during a {operation}")
        if self._info_index is not index:
            raise P4RuntimeError(f"P4Info changed during {operation}")

    def _translate_rpc_error(
        self,
        exc: grpc.aio.AioRpcError,
        *,
        pipeline: bool = False,
    ) -> P4RuntimeError:
        code = exc.code()
        details = exc.details() or ""
        if code == grpc.StatusCode.UNKNOWN:
            for canonical in _extract_p4_canonical_codes(exc):
                if canonical == 0:
                    continue
                resolved = _grpc_code_for(canonical)
                if resolved is not None:
                    code = resolved
                    if not details:
                        details = f"P4Runtime canonical_code={canonical}"
                    break
        if code == grpc.StatusCode.UNAVAILABLE:
            return ConnectionError(f"gRPC unavailable for {self.target!r}: {details}")
        if pipeline and code in (
            grpc.StatusCode.INVALID_ARGUMENT,
            grpc.StatusCode.FAILED_PRECONDITION,
        ):
            return PipelineError(f"pipeline rejected: {code} {details}")
        if code == grpc.StatusCode.ALREADY_EXISTS:
            return DuplicateEntryError(details)
        if code == grpc.StatusCode.NOT_FOUND:
            return EntryNotFoundError(details)
        if code == grpc.StatusCode.PERMISSION_DENIED:
            return NotPrimaryError(details)
        if code == grpc.StatusCode.CANCELLED:
            return AsyncOperationCancelledError(details)
        return P4RuntimeError(f"gRPC error {code}: {details}")


__all__ = ["AsyncP4RuntimeClient"]

# Keep alias-imported names alive for type checkers (unused at runtime).
_ = (Path, subprocess)
