"""Asynchronous P4Runtime client using ``grpc.aio``.

``AsyncP4RuntimeClient`` mirrors the public API surface of the sync
:class:`p4net.control.P4RuntimeClient`, but every method is ``async def``.
The two clients are independent: each owns its own gRPC channel, election
ID, and primary/secondary state. To run both clients against the same
BMv2 instance, callers must coordinate mastership explicitly (the sync
client wins by default because it usually connects first and grabs a
higher election ID via the millisecond-time-since-epoch generator).

**Provisional** in p4net 1.x. See ``docs/api-stability.md`` for the
upgrade-to-Stable timeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import grpc
from google.protobuf import text_format
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

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
from p4net.control.p4info_index import P4InfoIndex

logger = logging.getLogger(__name__)

_PIPELINE_ACTIONS: frozenset[str] = frozenset(
    {"VERIFY", "VERIFY_AND_SAVE", "VERIFY_AND_COMMIT", "COMMIT", "RECONCILE_AND_COMMIT"}
)


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
    """Async parallel to :class:`P4RuntimeClient`. **Provisional** in 1.x."""

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
        self._stub: Any = None
        self._outgoing: asyncio.Queue[Any] | None = None
        self._stream_call: Any = None
        self._stream_task: asyncio.Task[None] | None = None
        self._arbitration_event: asyncio.Event | None = None
        self._arbitration: Any = None
        self._stream_error: BaseException | None = None
        self._packet_in_queue: asyncio.Queue[tuple[bytes, dict[str, int]]] | None = None
        self._packet_handlers: list[Callable[[bytes, dict[str, int]], Awaitable[None]]] = []
        self._handlers_lock = asyncio.Lock()
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
        if self._connected:
            return
        self._closed = False
        self._channel = grpc.aio.insecure_channel(self.target)
        self._stub = p4runtime_pb2_grpc.P4RuntimeStub(self._channel)
        self._outgoing = asyncio.Queue()
        self._arbitration_event = asyncio.Event()
        self._arbitration = None
        self._stream_error = None
        self._packet_in_queue = asyncio.Queue()

        if self._election_id is None:
            millis = time.time_ns() // 1_000_000
            self._election_id = (int(millis), 0)

        self._stream_call = self._stub.StreamChannel(self._request_generator())
        self._stream_task = asyncio.create_task(
            self._stream_consumer(), name=f"p4rt-async-stream-{self._host}:{self._port}"
        )

        req = p4runtime_pb2.StreamMessageRequest()
        req.arbitration.device_id = self._device_id
        req.arbitration.election_id.high = self._election_id[0]
        req.arbitration.election_id.low = self._election_id[1]
        await self._outgoing.put(req)

        try:
            await asyncio.wait_for(self._arbitration_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await self._teardown()
            raise ConnectionError(
                f"P4Runtime async arbitration timed out after {timeout}s for {self.target!r}"
            ) from exc
        if self._stream_error is not None:
            err = self._stream_error
            await self._teardown()
            raise P4RuntimeError(f"async stream error: {err!r}") from err
        if self._arbitration is None:
            await self._teardown()
            raise ConnectionError("no arbitration response received")
        status_code = int(self._arbitration.status.code)
        self._is_primary = status_code == 0
        if status_code != 0 and self._election_id != (0, 0):
            # Explicit secondary (election_id=(0,0)) is allowed and stays connected.
            # Any other non-primary status is a failure.
            await self._teardown()
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
        if self._closed:
            return
        await self._teardown()

    async def _teardown(self) -> None:
        self._closed = True
        was_connected = self._connected
        self._connected = False
        self._is_primary = False
        if self._outgoing is not None:
            with self._suppress_queue_full():
                self._outgoing.put_nowait(None)  # sentinel
        if self._stream_call is not None:
            try:
                self._stream_call.cancel()
            except Exception as exc:
                logger.debug("async stream cancel raised: %r", exc)
        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await asyncio.shield(asyncio.wait_for(self._stream_task, timeout=2.0))
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as exc:
                logger.debug("async stream task awaited with: %r", exc)
        if self._channel is not None:
            try:
                await self._channel.close()
            except Exception as exc:
                logger.debug("async channel close raised: %r", exc)
        self._channel = None
        self._stub = None
        self._stream_call = None
        self._stream_task = None
        self._outgoing = None
        self._packet_in_queue = None
        if was_connected:
            logger.info("AsyncP4RuntimeClient %s disconnected", self.target)

    @staticmethod
    def _suppress_queue_full() -> Any:
        return contextlib.suppress(asyncio.QueueFull)

    async def __aenter__(self) -> AsyncP4RuntimeClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.disconnect()

    # ----------------------------------------------------------------- stream

    async def _request_generator(self) -> AsyncIterator[Any]:
        assert self._outgoing is not None
        while True:
            msg = await self._outgoing.get()
            if msg is None:
                return
            yield msg

    async def _stream_consumer(self) -> None:
        try:
            assert self._stream_call is not None
            async for resp in self._stream_call:
                if resp.HasField("arbitration"):
                    self._arbitration = resp.arbitration
                    assert self._arbitration_event is not None
                    self._arbitration_event.set()
                elif resp.HasField("packet"):
                    await self._dispatch_packet_in(resp.packet)
        except asyncio.CancelledError:
            raise
        except grpc.aio.AioRpcError as exc:
            if not self._closed:
                self._stream_error = exc
                if self._arbitration_event is not None:
                    self._arbitration_event.set()
        except Exception as exc:
            if not self._closed:
                self._stream_error = exc
                if self._arbitration_event is not None:
                    self._arbitration_event.set()

    async def _dispatch_packet_in(self, packet: Any) -> None:
        payload = bytes(packet.payload)
        meta: dict[str, int] = {}
        if self._info_index is not None:
            try:
                meta = self._info_index.decode_packet_in_metadata(packet.metadata)
            except Exception as exc:
                logger.debug("async decode_packet_in_metadata raised: %r", exc)
        # Buffer for expect_packet_in callers.
        if self._packet_in_queue is not None:
            with self._suppress_queue_full():
                self._packet_in_queue.put_nowait((payload, meta))
        # Dispatch to async handlers.
        async with self._handlers_lock:
            handlers = list(self._packet_handlers)
        for h in handlers:
            try:
                await h(payload, meta)
            except Exception as exc:
                logger.warning("async packet_in handler %r raised: %r", h, exc)

    # ----------------------------------------------------------------- pipeline

    async def push_pipeline(
        self,
        p4info_bytes: bytes,
        json_bytes: bytes,
        *,
        action: str = "VERIFY_AND_COMMIT",
        timeout: float = 10.0,
    ) -> None:
        """Push a compiled pipeline (raw bytes)."""
        self._require_connected()
        if action not in _PIPELINE_ACTIONS:
            raise P4RuntimeError(f"invalid pipeline action {action!r}")
        msg = p4info_pb2.P4Info()
        text_format.Merge(p4info_bytes.decode("utf-8"), msg)
        req = p4runtime_pb2.SetForwardingPipelineConfigRequest()
        req.device_id = self._device_id
        req.election_id.high = self._election_id[0]  # type: ignore[index]
        req.election_id.low = self._election_id[1]  # type: ignore[index]
        req.action = p4runtime_pb2.SetForwardingPipelineConfigRequest.Action.Value(action)
        req.config.p4info.CopyFrom(msg)
        req.config.p4_device_config = bytes(json_bytes)
        try:
            await asyncio.wait_for(self._stub.SetForwardingPipelineConfig(req), timeout=timeout)
        except asyncio.CancelledError as exc:
            raise AsyncOperationCancelledError("push_pipeline cancelled") from exc
        except grpc.aio.AioRpcError as exc:
            raise self._translate_rpc_error(exc, pipeline=True) from exc
        self._info_index = P4InfoIndex(msg)

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
        await self._write_entry(table, match, action, params, priority, "MODIFY", timeout)

    async def delete_table_entry(
        self,
        table: str,
        match: Mapping[str, object],
        *,
        priority: int | None = None,
        timeout: float = 5.0,
    ) -> None:
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
        idx = self.info_index
        table_id = idx.table_id(table)
        requires_priority = idx.table_requires_priority(table)
        if requires_priority and priority is None and update_type != "DELETE":
            raise EncodingError(f"table {table!r} has ternary/range fields; priority required")
        if not requires_priority and priority is not None:
            raise EncodingError(f"table {table!r} is exact/lpm-only; priority must be None")
        fms = idx.encode_match(table, match)
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = table_id
        for fm in fms:
            entry.match.add().CopyFrom(fm)
        if action is not None:
            entry.action.action.CopyFrom(idx.encode_action(action, params))
        if priority is not None:
            entry.priority = int(priority)
        upd = p4runtime_pb2.Update()
        upd.type = p4runtime_pb2.Update.Type.Value(update_type)
        upd.entity.table_entry.CopyFrom(entry)
        req = p4runtime_pb2.WriteRequest()
        req.device_id = self._device_id
        req.election_id.high = self._election_id[0]  # type: ignore[index]
        req.election_id.low = self._election_id[1]  # type: ignore[index]
        req.updates.add().CopyFrom(upd)
        try:
            await asyncio.wait_for(self._stub.Write(req), timeout=timeout)
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

        Note: this is a regular ``def`` returning an ``AsyncIterator``.
        Use it as ``async for entry in client.list_table_entries(...)``.
        """
        self._require_connected_with_index()
        idx = self.info_index
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self._device_id
        entity = req.entities.add()
        if table is not None:
            entity.table_entry.table_id = idx.table_id(table)
        else:
            entity.table_entry.table_id = 0

        async def _gen() -> AsyncIterator[dict[str, Any]]:
            try:
                async for resp in self._stub.Read(req, timeout=timeout):
                    for ent in resp.entities:
                        if ent.HasField("table_entry"):
                            yield self._decode_table_entry(ent.table_entry)
            except asyncio.CancelledError:
                raise
            except grpc.aio.AioRpcError as exc:
                raise self._translate_rpc_error(exc) from exc

        return _gen()

    def _decode_table_entry(self, entry: Any) -> dict[str, Any]:
        idx = self.info_index
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
        """Read indirect counter. Returns packet_count only for simplicity.

        Single-index returns the cell's packet_count (int). No index returns
        ``{cell_index: packet_count}``. The sync client returns the richer
        ``CounterData`` dataclass; the async API keeps things simple per spec.
        """
        self._require_connected_with_index()
        idx = self.info_index
        counter_id = idx.counter_id(counter)
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self._device_id
        entity = req.entities.add()
        entity.counter_entry.counter_id = counter_id
        if index is not None:
            entity.counter_entry.index.index = int(index)
        collected: dict[int, int] = {}
        try:
            async for resp in self._stub.Read(req, timeout=timeout):
                for ent in resp.entities:
                    if not ent.HasField("counter_entry"):
                        continue
                    ce = ent.counter_entry
                    collected[int(ce.index.index)] = int(ce.data.packet_count)
        except asyncio.CancelledError as exc:
            raise AsyncOperationCancelledError("read_counter cancelled") from exc
        except grpc.aio.AioRpcError as exc:
            raise self._translate_rpc_error(exc) from exc
        if index is not None:
            return collected.get(int(index), 0)
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
        self._require_connected_with_index()
        idx = self.info_index
        spec = idx.register_by_name(name)
        if not 0 <= int(index) < spec.size:
            raise EncodingError(f"register {name!r} index {index} out of range [0, {spec.size})")
        encode_value(int(value), spec.bitwidth)  # pre-flight bitwidth check
        await self._run_thrift_cli(
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
        self._require_connected_with_index()
        idx = self.info_index
        spec = idx.register_by_name(name)
        if index is not None and not 0 <= int(index) < spec.size:
            raise EncodingError(f"register {name!r} index {index} out of range [0, {spec.size})")
        if index is not None:
            output = await self._run_thrift_cli(
                f"register_read {name} {int(index)}",
                timeout=timeout,
                op_description=f"register_read {name}[{index}]",
            )
            return _parse_register_read_single(output, name, int(index))
        output = await self._run_thrift_cli(
            f"register_read {name}",
            timeout=timeout,
            op_description=f"register_read {name}",
        )
        return _parse_register_read_array(output, name, spec.size)

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
        try:
            proc = await asyncio.create_subprocess_exec(
                "simple_switch_CLI",
                "--thrift-ip",
                str(host),
                "--thrift-port",
                str(int(port)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise P4RuntimeError("simple_switch_CLI not found") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=(command + "\n").encode("utf-8")), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise P4RuntimeError(f"{op_description} timed out after {timeout}s") from exc
        except asyncio.CancelledError as exc:
            proc.kill()
            await proc.wait()
            raise AsyncOperationCancelledError(f"{op_description} cancelled") from exc
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
        """Await the next PacketIn. Raises P4RuntimeError on timeout."""
        self._require_connected()
        q = self._packet_in_queue
        if q is None:
            raise ConnectionError("packet_in queue is not initialised")
        try:
            return await asyncio.wait_for(q.get(), timeout=timeout)
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

    def _translate_rpc_error(
        self,
        exc: grpc.aio.AioRpcError,
        *,
        pipeline: bool = False,
    ) -> P4RuntimeError:
        code = exc.code()
        details = exc.details() or ""
        if pipeline:
            return PipelineError(f"pipeline rejected: {code} {details}")
        if code == grpc.StatusCode.ALREADY_EXISTS:
            return DuplicateEntryError(details)
        if code == grpc.StatusCode.NOT_FOUND:
            return EntryNotFoundError(details)
        if code in (grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.FAILED_PRECONDITION):
            return NotPrimaryError(details)
        if code == grpc.StatusCode.CANCELLED:
            return AsyncOperationCancelledError(details)
        return P4RuntimeError(f"gRPC error {code}: {details}")


__all__ = ["AsyncP4RuntimeClient"]

# Keep alias-imported names alive for type checkers (unused at runtime).
_ = (Path, subprocess)
