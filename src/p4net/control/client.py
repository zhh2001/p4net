"""P4Runtime gRPC client.

`P4RuntimeClient` owns one gRPC channel + StreamChannel for one P4Runtime
device. The stream is used for the master-arbitration handshake during
``connect()`` and is held open thereafter; future phases will route packet-in
/ packet-out / digest events through the same stream.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import grpc
from google.protobuf import text_format
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

from p4net.control._pipeline_state import _PipelineState
from p4net.control._rpc_errors import (
    _extract_p4_canonical_codes,
    _grpc_code_for,
    _grpc_status_code,
    _pipeline_change_rejected,
    _pipeline_not_configured,
)
from p4net.control.codec import encode_value
from p4net.control.exceptions import (
    ConnectionError,
    DuplicateEntryError,
    EncodingError,
    EntryNotFoundError,
    NotPrimaryError,
    P4RuntimeError,
    PipelineError,
)
from p4net.control.p4info_index import P4InfoIndex, _index_after_commit, _index_for_p4info

logger = logging.getLogger(__name__)

_DEFAULT_CHANNEL_OPTIONS: tuple[tuple[str, Any], ...] = (
    ("grpc.max_send_message_length", 16 * 1024 * 1024),
    ("grpc.max_receive_message_length", 16 * 1024 * 1024),
)

_SENTINEL = object()
_PIPELINE_ACTIONS: frozenset[str] = frozenset(
    {"VERIFY", "VERIFY_AND_SAVE", "VERIFY_AND_COMMIT", "COMMIT", "RECONCILE_AND_COMMIT"}
)
_PacketIn = tuple[bytes, dict[str, int]]


@dataclass(frozen=True)
class _PacketInClosed:
    cause: BaseException | None


class _PacketInWaiter:
    def __init__(self) -> None:
        self.result: queue.Queue[_PacketIn | _PacketInClosed] = queue.Queue(maxsize=1)

    def __call__(self, payload: bytes, metadata: dict[str, int]) -> None:
        self.notify((payload, metadata))

    def notify(self, result: _PacketIn | _PacketInClosed) -> None:
        with contextlib.suppress(queue.Full):
            self.result.put_nowait(result)


@dataclass(frozen=True)
class CounterData:
    """Decoded value of a P4 indirect counter cell."""

    packet_count: int
    byte_count: int


def _parse_register_read_single(output: str, name: str, index: int) -> int:
    """Parse ``register_read <name> <index>`` output: ``<name>[<index>]= <value>``."""
    needle = f"{name}["
    for raw_line in output.splitlines():
        line = raw_line.strip()
        # simple_switch_CLI emits lines prefixed with "RuntimeCmd: "; strip.
        if line.startswith("RuntimeCmd:"):
            line = line[len("RuntimeCmd:") :].strip()
        if line.startswith(needle):
            eq = line.find("=")
            if eq == -1:
                continue
            value_text = line[eq + 1 :].strip()
            try:
                return int(value_text)
            except ValueError as exc:
                raise P4RuntimeError(
                    f"register_read {name}[{index}]: could not parse value {value_text!r}"
                ) from exc
    raise P4RuntimeError(f"register_read {name}[{index}]: no value line in output: {output!r}")


def _parse_register_read_array(output: str, name: str, size: int) -> list[int]:
    """Parse ``register_read <name>`` output: ``<name>= v0, v1, v2, ...``."""
    needle = f"{name}="
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("RuntimeCmd:"):
            line = line[len("RuntimeCmd:") :].strip()
        if line.startswith(needle):
            value_text = line[len(needle) :].strip()
            try:
                values = [int(v.strip()) for v in value_text.split(",") if v.strip()]
            except ValueError as exc:
                raise P4RuntimeError(
                    f"register_read {name}: could not parse values from {value_text!r}"
                ) from exc
            if len(values) != size:
                raise P4RuntimeError(
                    f"register_read {name}: expected {size} cells, got {len(values)}"
                )
            return values
    raise P4RuntimeError(f"register_read {name}: no value line in output: {output!r}")


class P4RuntimeClient:
    """gRPC client for a single P4Runtime device.

    When the StreamChannel ends or fails, the client closes its channel
    and clears the connected state. Call :meth:`connect` to reconnect.
    Lifecycle calls wait for any channel cleanup already in progress.
    """

    def __init__(
        self,
        target: str,
        device_id: int,
        *,
        election_id: tuple[int, int] = (1, 0),
        role: str | None = None,
        channel_options: Sequence[tuple[str, object]] | None = None,
        thrift_address: tuple[str, int] | None = None,
    ) -> None:
        self._target = target
        self._device_id = int(device_id)
        self._election_id_high = int(election_id[0])
        self._election_id_low = int(election_id[1])
        self._role_name = role
        self._channel_options: list[tuple[str, Any]] = (
            list(channel_options) if channel_options is not None else list(_DEFAULT_CHANNEL_OPTIONS)
        )
        self._thrift_address = thrift_address

        self._channel: Any = None
        self._stub: Any = None
        self._outgoing: queue.Queue[Any] | None = None
        self._stream_call: Any = None
        self._stream_thread: threading.Thread | None = None
        self._connect_attempt: Future[None] | None = None
        self._stream_event = threading.Event()
        self._arbitration: Any = None
        self._stream_error: BaseException | None = None
        self._connected = False
        self._closed = False
        self._has_connected = False
        self._teardown_lock = threading.RLock()
        self._pipeline_lock = threading.Lock()
        self._active_indexed_operations = 0
        self._indexed_operations_idle = threading.Event()
        self._indexed_operations_idle.set()
        self._pipeline_state = _PipelineState()
        self._p4info_unconfirmed = False
        # Controller packet I/O. Handlers run on the stream-consumer thread.
        self._packet_in_handlers: list[Callable[[bytes, dict[str, int]], None]] = []
        self._packet_in_lock = threading.Lock()

    # Properties ---------------------------------------------------------

    @property
    def target(self) -> str:
        """gRPC target string (``host:port``) this client is bound to."""
        return self._target

    @property
    def device_id(self) -> int:
        """P4Runtime device ID this client identifies as."""
        return self._device_id

    @property
    def election_id(self) -> tuple[int, int]:
        """Mastership election ID as a ``(high, low)`` tuple."""
        return (self._election_id_high, self._election_id_low)

    @property
    def _index(self) -> P4InfoIndex | None:
        return None if self._p4info_unconfirmed else self._pipeline_state.index

    @_index.setter
    def _index(self, index: P4InfoIndex | None) -> None:
        self._pipeline_state.index = index

    @property
    def index(self) -> P4InfoIndex:
        """The cached :class:`P4InfoIndex` for the device's pipeline.

        Raises:
            P4RuntimeError: if no P4Info is cached.
        """
        index = self._index
        if index is None:
            raise P4RuntimeError(
                "no pipeline is set; call set_pipeline_config or get_pipeline_config first"
            )
        return index

    def is_connected(self) -> bool:
        """``True`` while the gRPC channel is open and arbitration succeeded."""
        return self._connected and not self._closed

    # Lifecycle ----------------------------------------------------------

    def connect(self, *, timeout: float = 10.0) -> None:
        """Connect using the first concurrent caller's timeout.

        A reconnect waits for arbitration and reads the current P4Info within
        that timeout. An unconfigured switch can connect with no cached index.
        During connection setup, a call from a PacketIn handler raises
        ``ConnectionError`` because the receiver thread must keep reading the stream.
        """
        attempt: Future[None] | None = None
        stream_call = None
        failed_thread = None
        try:
            with self._teardown_lock:
                if self._connected:
                    return
                pending = self._connect_attempt
                if pending is None:
                    attempt = self._connect_attempt = Future()
                    reconnect = self._has_connected
                    if reconnect:
                        self._p4info_unconfirmed = True
                    try:
                        stream_call, stream_event = self._start_stream_locked()
                    except BaseException:
                        failed_thread = self._close_stream_locked()
                        raise
                elif self._stream_thread is threading.current_thread():
                    raise ConnectionError("cannot wait for arbitration from a PacketIn handler")

            if pending is not None:
                pending.result()
                return
            assert attempt is not None
            deadline = time.monotonic() + timeout if reconnect else None
            self._await_arbitration(stream_call, stream_event, timeout=timeout)
            if deadline is not None:
                self._read_p4info_on_reconnect(stream_call, deadline=deadline)
            with self._teardown_lock:
                self._require_live_stream(
                    stream_call, "P4Runtime connection closed during connection setup"
                )
                logger.debug(
                    "P4Runtime client %r connected, election_id=(%d, %d)",
                    self._target,
                    self._election_id_high,
                    self._election_id_low,
                )
                self._p4info_unconfirmed = False
                self._has_connected = True
                self._connected = True
                self._connect_attempt = None
                attempt.set_result(None)
        except BaseException as exc:
            if attempt is not None:
                try:
                    if stream_call is not None:
                        self._teardown(expected_stream=stream_call)
                    self._join_stream_thread(failed_thread)
                finally:
                    with self._teardown_lock:
                        self._connect_attempt = None
                        attempt.set_exception(exc)
            raise

    def _await_arbitration(
        self,
        stream_call: Any,
        stream_event: threading.Event,
        *,
        timeout: float,
    ) -> None:
        if not stream_event.wait(timeout):
            raise ConnectionError(
                f"P4Runtime arbitration timed out after {timeout}s for {self._target!r}"
            )
        with self._teardown_lock:
            err = self._stream_error
            arbitration = self._arbitration
            active = stream_call is self._stream_call and not self._closed
            if active and err is None and arbitration is not None and arbitration.status.code == 0:
                return
        if err is not None:
            if isinstance(err, grpc.RpcError):
                raise self._translate_rpc_error(err) from err
            if isinstance(err, ConnectionError):
                raise err
            raise P4RuntimeError(f"stream error: {err!r}") from err
        if not active:
            raise ConnectionError("P4Runtime connection closed during arbitration")
        if arbitration is None:
            raise ConnectionError("no arbitration response received")
        raise NotPrimaryError(
            f"client is not primary for device {self._device_id} "
            f"(status code {int(arbitration.status.code)}, message {arbitration.status.message!r})"
        )

    def _read_p4info_on_reconnect(self, stream_call: Any, *, deadline: float) -> None:
        closed = f"Connection to {self.target!r} closed during P4Info readback"
        timed_out = f"{self.target!r} P4Info readback timed out"
        # A pipeline request from the previous connection may still be finishing.
        timeout = deadline - time.monotonic()
        if timeout <= 0 or not self._pipeline_lock.acquire(timeout=timeout):
            raise ConnectionError(timed_out)
        with contextlib.ExitStack() as pending:
            pending.callback(self._pipeline_lock.release)
            pending.enter_context(self._pipeline_state.access())
            with self._teardown_lock:
                self._require_live_stream(stream_call, closed)
                stub = self._stub
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise ConnectionError(timed_out)
            try:
                response = self._read_pipeline_config(
                    stub, response_type="P4INFO_AND_COOKIE", timeout=timeout
                )
            except (grpc.RpcError, ValueError) as exc:
                with self._teardown_lock:
                    if self._closed or stream_call is not self._stream_call:
                        raise ConnectionError(closed) from exc
                if isinstance(exc, ValueError):
                    raise
                if _pipeline_not_configured(exc):
                    with self._teardown_lock:
                        self._require_live_stream(stream_call, closed)
                        self._index = None
                    return
                if _grpc_status_code(exc) == grpc.StatusCode.DEADLINE_EXCEEDED:
                    raise ConnectionError(timed_out) from exc
                raise self._translate_rpc_error(exc) from exc
            with self._teardown_lock:
                self._require_live_stream(stream_call, closed)
                config = response.config
                self._index = (
                    _index_for_p4info(config.p4info, self._pipeline_state.index)
                    if config.HasField("p4info")
                    else None
                )

    def _require_live_stream(self, stream_call: Any, message: str) -> None:
        if self._closed or stream_call is not self._stream_call:
            raise ConnectionError(message)

    def _start_stream_locked(self) -> tuple[Any, threading.Event]:
        self._closed = False
        self._channel = grpc.insecure_channel(self._target, options=self._channel_options)
        self._stub = p4runtime_pb2_grpc.P4RuntimeStub(self._channel)
        outgoing = self._outgoing = queue.Queue()
        stream_event = self._stream_event = threading.Event()
        self._arbitration = None
        self._stream_error = None
        try:
            stream_call = self._stream_call = self._stub.StreamChannel(
                self._request_generator(outgoing)
            )
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc) from exc
        self._stream_thread = threading.Thread(
            target=self._stream_consumer, args=(stream_call,), name="p4rt-stream", daemon=True
        )
        self._stream_thread.start()

        req = p4runtime_pb2.StreamMessageRequest()
        req.arbitration.device_id = self._device_id
        req.arbitration.election_id.high = self._election_id_high
        req.arbitration.election_id.low = self._election_id_low
        if self._role_name:
            req.arbitration.role.name = self._role_name
        outgoing.put(req)
        return stream_call, stream_event

    def disconnect(self) -> None:
        """Close the stream channel and the gRPC channel. Idempotent."""
        self._teardown()

    def _teardown(self, *, expected_stream: Any = None) -> None:
        with self._teardown_lock:
            if expected_stream is not None and expected_stream is not self._stream_call:
                return
            was_connected = self._connected
            stream_thread = self._close_stream_locked()
        self._join_stream_thread(stream_thread)
        if was_connected:
            logger.debug("P4Runtime client %r disconnected", self._target)

    def _close_stream_locked(self) -> threading.Thread | None:
        self._closed = True
        if self._channel is not None and not self._connected:
            if self._stream_error is None:
                self._stream_error = ConnectionError(
                    f"P4Runtime connection closed before arbitration for {self._target!r}"
                )
            self._stream_event.set()
        self._connected = False
        with self._packet_in_lock:
            closed = _PacketInClosed(self._stream_error)
            for handler in self._packet_in_handlers:
                if isinstance(handler, _PacketInWaiter):
                    handler.notify(closed)
        if self._outgoing is not None:
            with contextlib.suppress(queue.Full):
                self._outgoing.put_nowait(_SENTINEL)
        if self._stream_call is not None:
            try:
                self._stream_call.cancel()
            except Exception as exc:
                logger.debug("stream cancel raised: %r", exc)
        stream_thread = self._stream_thread
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception as exc:
                logger.debug("channel close raised: %r", exc)
        self._channel = None
        self._stub = None
        self._stream_call = None
        self._stream_thread = None
        self._outgoing = None
        return stream_thread

    @staticmethod
    def _join_stream_thread(stream_thread: threading.Thread | None) -> None:
        # The consumer also takes the lock when it exits. Join after releasing it.
        if (
            stream_thread is not None
            and stream_thread is not threading.current_thread()
            and stream_thread.is_alive()
        ):
            stream_thread.join(timeout=2.0)

    def _request_generator(self, outgoing: queue.Queue[Any]) -> Iterator[Any]:
        while True:
            msg = outgoing.get()
            if msg is _SENTINEL:
                return
            yield msg

    def _stream_consumer(self, stream_call: Any) -> None:
        try:
            for resp in stream_call:
                with self._teardown_lock:
                    if self._closed or stream_call is not self._stream_call:
                        return
                    if resp.HasField("arbitration"):
                        self._arbitration = resp.arbitration
                        self._stream_event.set()
                if resp.HasField("packet"):
                    self._dispatch_packet_in(resp.packet, stream_call)
                # Future: digest / idle-notification / etc.
        except Exception as exc:
            with self._teardown_lock:
                if not self._closed and stream_call is self._stream_call:
                    self._stream_error = exc
        finally:
            with self._teardown_lock:
                if not self._closed and stream_call is self._stream_call:
                    if self._stream_error is None:
                        self._stream_error = ConnectionError(
                            f"P4Runtime stream ended for {self._target!r}"
                        )
                    self._stream_event.set()
                    self._teardown(expected_stream=stream_call)

    def _dispatch_packet_in(self, packet: Any, stream_call: Any) -> None:
        """Decode a PacketIn and fan out to registered handlers."""
        payload = bytes(packet.payload)
        metadata: dict[str, int] = {}
        index = None if self._p4info_unconfirmed else self._pipeline_state.packet_in_index()
        if index is not None:
            try:
                metadata = index.decode_packet_in_metadata(packet.metadata)
            except Exception as exc:
                logger.debug("decode_packet_in_metadata raised: %r", exc)
        with self._teardown_lock:
            if self._closed or stream_call is not self._stream_call:
                return
            with self._packet_in_lock:
                handlers = list(self._packet_in_handlers)
        for h in handlers:
            try:
                h(payload, metadata)
            except Exception as exc:
                logger.warning("packet_in handler %r raised: %r", h, exc)

    # Pipeline -----------------------------------------------------------

    def set_pipeline_config(
        self,
        *,
        bmv2_json: Path | None = None,
        p4info: Path | None = None,
        action: str = "VERIFY_AND_COMMIT",
        timeout: float = 10.0,
    ) -> None:
        """Push a compiled pipeline to the device.

        ``VERIFY`` validates the candidate and retains the current P4Info index.
        ``COMMIT`` uses the configuration already saved on the switch and reads
        back its P4Info. The file arguments are only used for other actions.
        The push waits for this client's in-flight table and counter
        operations. The timeout applies to each RPC.

        The client raises ``ConnectionError`` if the channel closes before
        dispatch or a successful reply arrives after disconnection.
        When the result of a dispatched pipeline change is uncertain, the
        client clears its cached P4Info. Use ``get_pipeline_config()`` to
        read the pipeline back and restore the index.
        """
        with self._teardown_lock:
            self._require_connected()
            stub = self._stub
        if action not in _PIPELINE_ACTIONS:
            raise P4RuntimeError(
                f"invalid pipeline action {action!r}; must be one of {sorted(_PIPELINE_ACTIONS)}"
            )
        req = p4runtime_pb2.SetForwardingPipelineConfigRequest()
        req.device_id = self._device_id
        if self._role_name:
            req.role = self._role_name
        req.election_id.high = self._election_id_high
        req.election_id.low = self._election_id_low
        req.action = p4runtime_pb2.SetForwardingPipelineConfigRequest.Action.Value(action)
        p4info_msg = None
        if action != "COMMIT":
            if p4info is None or bmv2_json is None:
                raise TypeError(f"p4info and bmv2_json are required for {action}")
            p4info_msg = p4info_pb2.P4Info()
            text_format.Merge(Path(p4info).read_text(), p4info_msg)
            req.config.p4info.CopyFrom(p4info_msg)
            req.config.p4_device_config = Path(bmv2_json).read_bytes()

        with self._pipeline_lock, contextlib.ExitStack() as pipeline:
            self._indexed_operations_idle.wait()
            pipeline.enter_context(self._pipeline_state.access(change=action != "VERIFY"))
            with self._teardown_lock:
                self._require_same_connection(stub, operation="a pipeline operation")
                previous = self._index
            discard_index = action != "VERIFY"
            try:
                try:
                    stub.SetForwardingPipelineConfig(req, timeout=timeout)
                except grpc.RpcError as exc:
                    discard_index = discard_index and not _pipeline_change_rejected(exc)
                    raise self._translate_rpc_error(exc, pipeline=True) from exc
                except (TypeError, ValueError) as exc:
                    discard_index = False
                    # gRPC can reject a call locally after the channel has closed.
                    if isinstance(exc, ValueError):
                        with self._teardown_lock:
                            self._require_same_connection(stub, operation="a pipeline operation")
                    raise
                if p4info_msg is None:
                    self._refresh_committed_pipeline(stub, timeout=timeout)
                    p4info_msg = self.index.raw
                else:
                    with self._teardown_lock:
                        self._require_same_connection(stub, operation="a pipeline operation")
                        if action != "VERIFY":
                            self._index = P4InfoIndex(p4info_msg)
                discard_index = False
            finally:
                if discard_index:
                    with self._teardown_lock:
                        if self._pipeline_state.index is previous:
                            self._index = None
            logger.debug(
                "P4Runtime pipeline request succeeded (action=%s, p4info_tables=%d)",
                action,
                len(p4info_msg.tables),
            )

    def _refresh_committed_pipeline(self, stub: Any, *, timeout: float) -> None:
        connection_closed = "COMMIT succeeded. Connection closed during P4Info refresh."
        with self._teardown_lock:
            if self._stub is not stub or not self.is_connected():
                raise ConnectionError(connection_closed)
            previous, self._index = self._index, None
        try:
            response = self._read_pipeline_config(
                stub, response_type="P4INFO_AND_COOKIE", timeout=timeout
            )
        except grpc.RpcError as exc:
            error = self._translate_rpc_error(exc)
            raise type(error)(f"COMMIT succeeded. P4Info refresh failed: {error}") from exc
        except ValueError as exc:
            with self._teardown_lock:
                if self._stub is not stub or not self.is_connected():
                    raise ConnectionError(connection_closed) from exc
            raise
        with self._teardown_lock:
            if self._stub is not stub or not self.is_connected():
                raise ConnectionError(connection_closed)
            self._index = _index_after_commit(response.config, previous)

    def get_pipeline_config(self, *, timeout: float = 10.0) -> tuple[bytes, P4InfoIndex]:
        """Read the current pipeline back from the device.

        The client keeps its cached index when the returned P4Info matches.
        A response without P4Info clears the cached index and raises
        ``P4RuntimeError``. The device configuration bytes may be empty.
        The client raises ``ConnectionError`` if the channel closes before
        dispatch or a successful reply arrives after disconnection.
        """
        with self._teardown_lock:
            self._require_connected()
            stub = self._stub
        with self._pipeline_lock, self._pipeline_state.access():
            with self._teardown_lock:
                self._require_same_connection(stub, operation="a pipeline operation")
            try:
                response = self._read_pipeline_config(stub, response_type="ALL", timeout=timeout)
            except grpc.RpcError as exc:
                raise self._translate_rpc_error(exc) from exc
            except ValueError:
                with self._teardown_lock:
                    self._require_same_connection(stub, operation="a pipeline operation")
                raise
            with self._teardown_lock:
                self._require_same_connection(stub, operation="a pipeline operation")
                config = response.config
                if not config.HasField("p4info"):
                    self._index = None
                    if not response.HasField("config"):
                        raise P4RuntimeError(
                            f"Device {self.target!r} returned no forwarding pipeline configuration"
                        )
                    raise P4RuntimeError(
                        f"Device {self.target!r} returned no P4Info for its pipeline"
                    )
                index = _index_for_p4info(config.p4info, self._index)
                self._index = index
        return config.p4_device_config, index

    def _require_same_connection(self, stub: Any, *, operation: str) -> None:
        if self._stub is not stub or not self.is_connected():
            raise ConnectionError(f"Connection to {self.target!r} closed during {operation}")

    def _require_same_indexed_context(
        self, stub: Any, index: P4InfoIndex, *, operation: str
    ) -> None:
        self._require_same_connection(stub, operation=f"a {operation}")
        if self._index is not index:
            raise P4RuntimeError(f"P4Info changed during {operation}")

    def _read_pipeline_config(self, stub: Any, *, response_type: str, timeout: float) -> Any:
        req = p4runtime_pb2.GetForwardingPipelineConfigRequest()
        req.device_id = self._device_id
        req.response_type = p4runtime_pb2.GetForwardingPipelineConfigRequest.ResponseType.Value(
            response_type
        )
        return stub.GetForwardingPipelineConfig(req, timeout=timeout)

    # Table CRUD ---------------------------------------------------------

    def insert_table_entry(
        self,
        table: str,
        match: Mapping[str, object],
        action: str,
        params: Mapping[str, object] | None = None,
        *,
        priority: int | None = None,
        timeout: float = 5.0,
    ) -> None:
        """Insert a new entry into ``table``.

        Args:
            table: Fully qualified table name (e.g. ``MyIngress.ipv4_lpm``).
            match: ``{field_name: value}`` for the match key. Values may be
                IPv4/IPv6/MAC strings, decimal/hex integers, or raw bytes.
                LPM fields take ``"<addr>/<plen>"``; ternary fields take
                ``("<value>", "<mask>")``.
            action: Fully qualified action name.
            params: Action parameters keyed by P4 param name.
            priority: Required for tables with optional, ternary or range keys,
                including entries that omit optional fields as wildcards. Must
                be an int in [1, 2147483647]; bool is not accepted.
            timeout: Per-call gRPC deadline in seconds.

        Raises:
            DuplicateEntryError: if an entry with the same key already exists.
            EncodingError: if any field can't be encoded.
            ConnectionError: if the connection closes during request preparation.
            P4RuntimeError: if P4Info changes during request preparation.
        """
        self._write_entry(
            table,
            match,
            action,
            params,
            priority=priority,
            update_type="INSERT",
            timeout=timeout,
        )

    def modify_table_entry(
        self,
        table: str,
        match: Mapping[str, object],
        action: str,
        params: Mapping[str, object] | None = None,
        *,
        priority: int | None = None,
        timeout: float = 5.0,
    ) -> None:
        """Modify an existing entry in ``table``. Same arguments as
        :meth:`insert_table_entry`. Raises :class:`EntryNotFoundError`
        if no matching entry exists."""
        self._write_entry(
            table,
            match,
            action,
            params,
            priority=priority,
            update_type="MODIFY",
            timeout=timeout,
        )

    def delete_table_entry(
        self,
        table: str,
        match: Mapping[str, object],
        *,
        priority: int | None = None,
        timeout: float = 5.0,
    ) -> None:
        """Delete an entry from ``table`` by match key and original priority.

        Tables with optional, ternary or range keys require ``priority``,
        including wildcard entries. Use an int in [1, 2147483647], excluding
        bool. Omit it for exact/LPM-only tables.

        Raises:
            EncodingError: if a required priority is missing or a match is invalid.
            EntryNotFoundError: if no matching entry exists.
        """
        self._write_entry(
            table,
            match,
            action=None,
            params=None,
            priority=priority,
            update_type="DELETE",
            timeout=timeout,
        )

    def _write_entry(
        self,
        table: str,
        match: Mapping[str, object],
        action: str | None,
        params: Mapping[str, object] | None,
        *,
        priority: int | None,
        update_type: str,
        timeout: float,
    ) -> None:
        stub, index = self._indexed_context()
        table_id = index.table_id(table)
        requires_priority = index.table_requires_priority(table)
        if requires_priority and priority is None:
            raise EncodingError(
                f"table {table!r} has optional/ternary/range match fields; priority is required"
            )
        if not requires_priority and priority is not None:
            raise EncodingError(f"table {table!r} is exact/lpm-only; priority must be None")
        if priority is not None and (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or not 1 <= priority <= 0x7FFFFFFF
        ):
            raise EncodingError("priority must be an int in [1, 2147483647] (bool is not accepted)")
        field_matches = index.encode_match(table, match)
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = table_id
        for fm in field_matches:
            entry.match.add().CopyFrom(fm)
        if action is not None:
            entry.action.action.CopyFrom(index.encode_action(action, params))
        if priority is not None:
            entry.priority = priority

        update = p4runtime_pb2.Update()
        update.type = p4runtime_pb2.Update.Type.Value(update_type)
        update.entity.table_entry.CopyFrom(entry)

        req = p4runtime_pb2.WriteRequest()
        req.device_id = self._device_id
        if self._role_name:
            req.role = self._role_name
        req.election_id.high = self._election_id_high
        req.election_id.low = self._election_id_low
        req.updates.add().CopyFrom(update)

        self._write_indexed_request(stub, index, req, timeout=timeout, operation="table write")
        logger.debug(
            "P4Runtime %s table_entry table=%s match=%s action=%s",
            update_type,
            table,
            dict(match),
            action,
        )

    def _indexed_context(self) -> tuple[Any, P4InfoIndex]:
        with self._teardown_lock:
            self._require_connected()
            return self._stub, self.index

    @contextlib.contextmanager
    def _indexed_operation(
        self, stub: Any, index: P4InfoIndex, *, operation: str
    ) -> Iterator[None]:
        # Register under the pipeline lock so a push can wait for these RPCs
        # while reads and writes can still overlap.
        with contextlib.ExitStack() as active:
            with self._pipeline_lock, self._teardown_lock:
                active.enter_context(self._pipeline_state.access())
                self._require_same_indexed_context(stub, index, operation=operation)
                self._active_indexed_operations += 1
                self._indexed_operations_idle.clear()
            try:
                yield
            finally:
                active.close()
                with self._teardown_lock:
                    self._active_indexed_operations -= 1
                    if self._active_indexed_operations == 0:
                        self._indexed_operations_idle.set()

    def _write_indexed_request(
        self, stub: Any, index: P4InfoIndex, req: Any, *, timeout: float, operation: str
    ) -> None:
        with self._indexed_operation(stub, index, operation=operation):
            try:
                stub.Write(req, timeout=timeout)
            except grpc.RpcError as exc:
                raise self._translate_rpc_error(exc) from exc
            except ValueError:
                # gRPC can report a closed channel if teardown wins after the check.
                with self._teardown_lock:
                    self._require_same_connection(stub, operation=f"a {operation}")
                raise

    def list_table_entries(
        self,
        table: str | None = None,
        *,
        timeout: float = 5.0,
    ) -> list[dict[str, Any]]:
        """Return decoded entries for one table, or all tables if `table` is None.

        The byte values in each entry's ``match`` mapping are returned in
        P4Runtime canonical form (P4Runtime spec §8.4): they may be shorter
        than the bitwidth-rounded width because leading zero bytes are
        stripped. They round-trip correctly through ``insert_table_entry``,
        ``modify_table_entry``, and ``delete_table_entry`` because
        ``encode_value`` accepts shorter ``bytes`` inputs.

        Raises ``ConnectionError`` if the connection closes during the read
        and ``P4RuntimeError`` if P4Info changes during the read.
        """
        stub, index = self._indexed_context()
        return self._read_table_entries(stub, index, table, timeout=timeout)

    def _read_table_entries(
        self, stub: Any, index: P4InfoIndex, table: str | None, *, timeout: float
    ) -> list[dict[str, Any]]:
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self._device_id
        entity = req.entities.add()
        if table is not None:
            entity.table_entry.table_id = index.table_id(table)
        else:
            entity.table_entry.table_id = 0
        with self._indexed_operation(stub, index, operation="table read"):
            try:
                response_iter = stub.Read(req, timeout=timeout)
                entries: list[dict[str, Any]] = []
                for resp in response_iter:
                    for ent in resp.entities:
                        if ent.HasField("table_entry"):
                            entries.append(self._decode_table_entry(ent.table_entry, index=index))
            except grpc.RpcError as exc:
                raise self._translate_rpc_error(exc) from exc
            except (P4RuntimeError, ValueError):
                with self._teardown_lock:
                    self._require_same_indexed_context(stub, index, operation="table read")
                raise
            with self._teardown_lock:
                self._require_same_indexed_context(stub, index, operation="table read")
            return entries

    def clear_table(self, table: str, *, timeout: float = 10.0) -> int:
        """Read and delete all entries from `table` on the same connection.

        Return the number of deleted entries.
        The read and delete use the same P4Info.
        If the connection or P4Info changes during the read, even an empty
        response raises an error.
        """
        stub, index = self._indexed_context()
        entries = self._read_table_entries(stub, index, table, timeout=timeout)
        if not entries:
            return 0
        table_id = index.table_id(table)
        req = p4runtime_pb2.WriteRequest()
        req.device_id = self._device_id
        if self._role_name:
            req.role = self._role_name
        req.election_id.high = self._election_id_high
        req.election_id.low = self._election_id_low
        for entry in entries:
            te = p4runtime_pb2.TableEntry()
            te.table_id = table_id
            # Re-encode the match fields. The decoded form holds raw bytes;
            # we feed those back as bytes into encode_value, which is fine.
            field_matches = index.encode_match(table, entry["match"])
            for fm in field_matches:
                te.match.add().CopyFrom(fm)
            if entry.get("priority") is not None:
                te.priority = int(entry["priority"])
            update = req.updates.add()
            update.type = p4runtime_pb2.Update.Type.Value("DELETE")
            update.entity.table_entry.CopyFrom(te)
        self._write_indexed_request(stub, index, req, timeout=timeout, operation="table write")
        return len(entries)

    def _decode_table_entry(
        self, entry: Any, *, index: P4InfoIndex | None = None
    ) -> dict[str, Any]:
        if index is None:
            index = self.index
        table_name = index.table_name(int(entry.table_id))
        table = index.raw.tables[0]
        for t in index.raw.tables:
            if t.preamble.id == entry.table_id:
                table = t
                break
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
            action_name = index.action_name(int(a.action_id))
            for action_msg in index.raw.actions:
                if action_msg.preamble.id == a.action_id:
                    params_by_id = {int(p.id): p for p in action_msg.params}
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

    # Counters -----------------------------------------------------------

    def read_counter(
        self,
        counter: str,
        index: int | None = None,
        *,
        timeout: float = 5.0,
    ) -> CounterData | dict[int, CounterData]:
        """Read one or all populated cells of an indirect counter.

        The read uses the connection and P4Info selected at the start of the call.
        """
        stub, p4info = self._indexed_context()
        counter_id = p4info.counter_id(counter)
        cell = int(index) if index is not None else None
        collected = self._read_counter_cells(stub, p4info, counter_id, cell, timeout=timeout)
        if cell is not None:
            return collected.get(cell, CounterData(0, 0))
        return collected

    def _read_counter_cells(
        self, stub: Any, p4info: P4InfoIndex, counter_id: int, cell: int | None, *, timeout: float
    ) -> dict[int, CounterData]:
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self._device_id
        entity = req.entities.add()
        entity.counter_entry.counter_id = counter_id
        if cell is not None:
            entity.counter_entry.index.index = cell
        with self._indexed_operation(stub, p4info, operation="counter read"):
            try:
                response_iter = stub.Read(req, timeout=timeout)
                collected: dict[int, CounterData] = {}
                for resp in response_iter:
                    for ent in resp.entities:
                        if not ent.HasField("counter_entry"):
                            continue
                        ce = ent.counter_entry
                        cell_index = int(ce.index.index)
                        collected[cell_index] = CounterData(
                            packet_count=int(ce.data.packet_count),
                            byte_count=int(ce.data.byte_count),
                        )
            except grpc.RpcError as exc:
                raise self._translate_rpc_error(exc) from exc
            except ValueError:
                with self._teardown_lock:
                    self._require_same_connection(stub, operation="a counter read")
                raise
            with self._teardown_lock:
                self._require_same_indexed_context(stub, p4info, operation="counter read")
            return collected

    def reset_counter(
        self,
        counter: str,
        index: int | None = None,
        *,
        timeout: float = 5.0,
    ) -> None:
        """Request zero values for one indirect counter cell or all returned cells.

        BMv2 applies the reset to the fields selected by the P4Info counter unit.
        A batch reset reads and writes through the same connection.
        """
        stub, p4info = self._indexed_context()
        counter_id = p4info.counter_id(counter)
        targets: list[int]
        if index is None:
            current = self._read_counter_cells(stub, p4info, counter_id, None, timeout=timeout)
            targets = sorted(current)
            if not targets:
                return
        else:
            targets = [int(index)]
        req = p4runtime_pb2.WriteRequest()
        req.device_id = self._device_id
        if self._role_name:
            req.role = self._role_name
        req.election_id.high = self._election_id_high
        req.election_id.low = self._election_id_low
        for cell_idx in targets:
            update = req.updates.add()
            update.type = p4runtime_pb2.Update.Type.Value("MODIFY")
            ce = update.entity.counter_entry
            ce.counter_id = counter_id
            ce.index.index = int(cell_idx)
            ce.data.packet_count = 0
            ce.data.byte_count = 0
        self._write_indexed_request(stub, p4info, req, timeout=timeout, operation="counter reset")

    # Registers ----------------------------------------------------------

    # BMv2's P4Runtime backend currently returns UNIMPLEMENTED for RegisterEntry
    # over gRPC ("Register reads are not supported yet" in libpifeproto). The
    # P4Runtime contract is honored at the Python API surface — same method
    # names and semantics — but the implementation delegates to BMv2's Thrift
    # control channel via ``simple_switch_CLI``. Targets with a compliant
    # P4Runtime RegisterEntry implementation can be migrated by swapping the
    # transport here without changing the public API.

    def write_register(
        self,
        name: str,
        index: int,
        value: int,
        *,
        timeout: float = 5.0,
    ) -> None:
        """Write a single cell of a P4 register.

        Args:
            name: Fully qualified P4 register name
                (e.g. ``MyIngress.switch_id``).
            index: Cell index in the register array. Must be in range
                ``[0, size)``.
            value: Integer value to write. Must fit in the register's
                bitwidth.

        Raises:
            NoSuchRegisterError: if the register doesn't exist.
            EncodingError: if ``index`` is out of range or ``value``
                exceeds the register's bitwidth.
            ConnectionError: if the connection closes during preparation.
            P4RuntimeError: if the underlying control channel returns
                an error, no Thrift address is configured, or P4Info changes
                during preparation.
        """
        stub, p4info = self._indexed_context()
        spec = p4info.register_by_name(name)
        if not 0 <= int(index) < spec.size:
            raise EncodingError(f"register {name!r} index {index} out of range [0, {spec.size})")
        # Force bitwidth validation. Result bytes are unused — the Thrift
        # CLI takes a decimal integer.
        encode_value(int(value), spec.bitwidth)
        self._run_register_command(
            stub,
            p4info,
            f"register_write {name} {int(index)} {int(value)}",
            timeout=timeout,
            op_description=f"register_write {name}[{index}]",
        )
        logger.debug(
            "BMv2 thrift register_write %s[%d] = %d (bitwidth=%d)",
            name,
            int(index),
            int(value),
            spec.bitwidth,
        )

    def read_register(
        self,
        name: str,
        index: int | None = None,
        *,
        timeout: float = 5.0,
    ) -> int | list[int]:
        """Read a P4 register.

        Args:
            name: Fully qualified P4 register name.
            index: Cell index, or ``None`` to read every cell.

        Returns:
            If ``index`` is given: the integer value of that cell.
            If ``index`` is ``None``: a list of integers indexed by
            position ``[0, size)``. Cells default to ``0`` (BMv2
            initializes register elements to zero).

        Raises:
            NoSuchRegisterError: if the register doesn't exist.
            EncodingError: if ``index`` is out of range.
            ConnectionError: if the connection closes during preparation.
            P4RuntimeError: if the underlying control channel returns
                an error, no Thrift address is configured, or P4Info changes
                during preparation.
        """
        stub, p4info = self._indexed_context()
        spec = p4info.register_by_name(name)
        if index is not None and not 0 <= int(index) < spec.size:
            raise EncodingError(f"register {name!r} index {index} out of range [0, {spec.size})")
        if index is not None:
            output = self._run_register_command(
                stub,
                p4info,
                f"register_read {name} {int(index)}",
                timeout=timeout,
                op_description=f"register_read {name}[{index}]",
            )
            return _parse_register_read_single(output, name, int(index))
        output = self._run_register_command(
            stub,
            p4info,
            f"register_read {name}",
            timeout=timeout,
            op_description=f"register_read {name}",
        )
        return _parse_register_read_array(output, name, spec.size)

    def _run_register_command(
        self,
        stub: Any,
        p4info: P4InfoIndex,
        command: str,
        *,
        timeout: float,
        op_description: str,
    ) -> str:
        # The CLI opens its own connection, so keep this client's connection
        # and pipeline stable until the command completes.
        with self._pipeline_lock, self._pipeline_state.access(), self._teardown_lock:
            self._require_same_indexed_context(stub, p4info, operation="register operation")
            return self._run_thrift_cli(command, timeout=timeout, op_description=op_description)

    def _run_thrift_cli(
        self,
        command: str,
        *,
        timeout: float,
        op_description: str,
    ) -> str:
        """Run a single command against ``simple_switch_CLI`` and return stdout."""
        if self._thrift_address is None:
            raise P4RuntimeError(
                "register operations require a Thrift sidecar address; "
                f"construct P4RuntimeClient with thrift_address=(host, port). "
                f"Operation: {op_description}"
            )
        host, port = self._thrift_address
        import subprocess

        try:
            result = subprocess.run(
                [
                    "simple_switch_CLI",
                    "--thrift-ip",
                    str(host),
                    "--thrift-port",
                    str(int(port)),
                ],
                input=command + "\n",
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise P4RuntimeError(
                "simple_switch_CLI not found on PATH; required for register operations"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise P4RuntimeError(
                f"{op_description} timed out after {timeout}s against thrift {host}:{port}"
            ) from exc
        combined = result.stdout + result.stderr
        if result.returncode != 0:
            raise P4RuntimeError(
                f"{op_description} failed (rc={result.returncode}): {combined.strip()}"
            )
        for line in combined.splitlines():
            stripped = line.strip()
            if stripped.startswith("Error:") or "Invalid" in stripped:
                raise P4RuntimeError(f"{op_description}: {stripped}")
        return result.stdout

    # Multicast groups ---------------------------------------------------

    def add_multicast_group(
        self,
        group_id: int,
        ports: Sequence[int],
        *,
        timeout: float = 5.0,
    ) -> None:
        """Create a multicast group with one replica per port (instance=1).

        Raises ``ConnectionError`` if the connection closes during request preparation.
        """
        self._mcast_write(group_id, ports, update_type="INSERT", timeout=timeout)

    def modify_multicast_group(
        self,
        group_id: int,
        ports: Sequence[int],
        *,
        timeout: float = 5.0,
    ) -> None:
        """Replace the replica list of an existing multicast group.

        Raises ``ConnectionError`` if the connection closes during request preparation.
        """
        self._mcast_write(group_id, ports, update_type="MODIFY", timeout=timeout)

    def delete_multicast_group(
        self,
        group_id: int,
        *,
        timeout: float = 5.0,
    ) -> None:
        """Delete a multicast group.

        Raises ``ConnectionError`` if the connection closes during request preparation.
        """
        self._mcast_write(group_id, ports=(), update_type="DELETE", timeout=timeout)

    def _mcast_write(
        self,
        group_id: int,
        ports: Sequence[int],
        *,
        update_type: str,
        timeout: float,
    ) -> None:
        with self._teardown_lock:
            self._require_connected()
            stub = self._stub
        if group_id <= 0:
            raise EncodingError(f"multicast group_id must be positive, got {group_id}")
        req = p4runtime_pb2.WriteRequest()
        req.device_id = self._device_id
        if self._role_name:
            req.role = self._role_name
        req.election_id.high = self._election_id_high
        req.election_id.low = self._election_id_low
        update = req.updates.add()
        update.type = p4runtime_pb2.Update.Type.Value(update_type)
        mge = update.entity.packet_replication_engine_entry.multicast_group_entry
        mge.multicast_group_id = int(group_id)
        if update_type != "DELETE":
            for port in ports:
                replica = mge.replicas.add()
                replica.egress_port = int(port)
                replica.instance = 1
        with self._teardown_lock:
            self._require_same_connection(stub, operation="a multicast write")
        try:
            stub.Write(req, timeout=timeout)
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc) from exc
        except ValueError:
            with self._teardown_lock:
                self._require_same_connection(stub, operation="a multicast write")
            raise

    def list_multicast_groups(self, *, timeout: float = 5.0) -> dict[int, list[int]]:
        """Return ``{group_id: [egress_port, ...]}``.

        Replica instance numbers are flattened away — each port appears once
        per replica regardless of its instance value (we always write
        instance=1 ourselves; foreign instance values are still listed but
        not exposed in this dict shape).

        Raises ``ConnectionError`` if the connection closes during the read.
        """
        with self._teardown_lock:
            self._require_connected()
            stub = self._stub
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self._device_id
        entity = req.entities.add()
        entity.packet_replication_engine_entry.multicast_group_entry.multicast_group_id = 0
        with self._teardown_lock:
            self._require_same_connection(stub, operation="a multicast read")
        try:
            response_iter = stub.Read(req, timeout=timeout)
            groups: dict[int, list[int]] = {}
            for resp in response_iter:
                for ent in resp.entities:
                    if not ent.HasField("packet_replication_engine_entry"):
                        continue
                    pre = ent.packet_replication_engine_entry
                    if not pre.HasField("multicast_group_entry"):
                        continue
                    mge = pre.multicast_group_entry
                    groups[int(mge.multicast_group_id)] = [int(r.egress_port) for r in mge.replicas]
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc) from exc
        except ValueError:
            with self._teardown_lock:
                self._require_same_connection(stub, operation="a multicast read")
            raise
        with self._teardown_lock:
            self._require_same_connection(stub, operation="a multicast read")
        return groups

    # Controller packet I/O ---------------------------------------------

    def send_packet_out(
        self,
        payload: bytes,
        metadata: Mapping[str, object] | None = None,
        *,
        timeout: float = 5.0,
    ) -> None:
        """Send a PacketOut over the StreamChannel.

        ``payload`` is the full packet to inject — controller headers are
        rebuilt from ``metadata`` per the loaded P4Info. PacketOut is
        fire-and-forget in P4Runtime; this method does not wait for a switch
        response. ``timeout`` is reserved for future flow-control limits and
        currently only bounds the queue put.

        Raises:
            ConnectionError: if the connection closes during packet preparation.
            P4RuntimeError: if P4Info changes during packet preparation.
        """
        with self._teardown_lock:
            self._require_connected()
            out, idx = self._outgoing, self._index
        if out is None:
            raise ConnectionError("client is not connected; outgoing queue is closed")
        encoded = idx.encode_packet_out_metadata(metadata or {}) if idx is not None else []
        if metadata and idx is None:
            raise EncodingError(
                "no pipeline is set; cannot encode packet_out metadata "
                "(call set_pipeline_config or get_pipeline_config first)"
            )
        if not isinstance(payload, (bytes, bytearray)):
            raise EncodingError(f"payload must be bytes-like, got {type(payload).__name__}")
        req = p4runtime_pb2.StreamMessageRequest()
        req.packet.payload = bytes(payload)
        for pm in encoded:
            req.packet.metadata.add().CopyFrom(pm)
        with self._pipeline_state.access(), self._teardown_lock:
            if self._outgoing is not out or not self.is_connected():
                raise ConnectionError(
                    f"Connection to {self.target!r} closed during PacketOut preparation"
                )
            if self._index is not idx:
                raise P4RuntimeError("P4Info changed during PacketOut preparation")
            out.put(req, timeout=timeout)

    def on_packet_in(
        self,
        handler: Callable[[bytes, dict[str, int]], None],
    ) -> Callable[[], None]:
        """Register a PacketIn handler. Returns a deregister function.

        Handlers run on the StreamChannel consumer thread. Multiple handlers
        are invoked in registration order; an exception from one is logged
        and does not prevent later handlers from running. The returned
        deregister function tolerates double-unregister silently.
        """
        with self._packet_in_lock:
            self._packet_in_handlers.append(handler)

        def deregister() -> None:
            with self._packet_in_lock, contextlib.suppress(ValueError):
                self._packet_in_handlers.remove(handler)

        return deregister

    def expect_packet_in(
        self,
        *,
        timeout: float = 5.0,
    ) -> tuple[bytes, dict[str, int]]:
        """Wait for the next PacketIn on the current connection.

        Raises ConnectionError if the connection closes during the wait,
        or P4RuntimeError on timeout.
        """
        waiter = _PacketInWaiter()
        with self._teardown_lock:
            self._require_connected()
            deregister = self.on_packet_in(waiter)
        try:
            result = waiter.result.get(timeout=timeout)
        except queue.Empty as exc:
            raise P4RuntimeError(f"no PacketIn within {timeout}s on {self._target!r}") from exc
        finally:
            deregister()
        if isinstance(result, _PacketInClosed):
            raise ConnectionError(
                f"Connection to {self.target!r} closed while waiting for PacketIn"
            ) from result.cause
        return result

    # Internals ----------------------------------------------------------

    def _require_connected(self) -> None:
        if not self.is_connected():
            raise ConnectionError("client is not connected; call connect() first")

    def _translate_rpc_error(self, exc: grpc.RpcError, *, pipeline: bool = False) -> P4RuntimeError:
        try:
            code = exc.code()
        except Exception:
            return P4RuntimeError(f"gRPC error: {exc!r}")
        try:
            detail = exc.details() or ""
        except Exception:
            detail = ""
        # P4Runtime batches per-update statuses inside an outer UNKNOWN gRPC
        # error; the per-update canonical_code is encoded as a `p4.v1.Error`
        # entry in `grpc-status-details-bin`. Resolve to the real code so
        # callers see DuplicateEntryError / EntryNotFoundError instead of a
        # generic UNKNOWN.
        if code == grpc.StatusCode.UNKNOWN:
            for canonical in _extract_p4_canonical_codes(exc):
                if canonical == 0:
                    continue
                resolved = _grpc_code_for(canonical)
                if resolved is not None:
                    code = resolved
                    if not detail:
                        detail = f"P4Runtime canonical_code={canonical}"
                    break
        if code == grpc.StatusCode.UNAVAILABLE:
            return ConnectionError(f"gRPC unavailable for {self._target!r}: {detail}")
        if code == grpc.StatusCode.PERMISSION_DENIED:
            return NotPrimaryError(detail or "client is not primary")
        if code == grpc.StatusCode.NOT_FOUND:
            return EntryNotFoundError(detail or "entry not found")
        if code == grpc.StatusCode.ALREADY_EXISTS:
            return DuplicateEntryError(detail or "entry already exists")
        if code == grpc.StatusCode.FAILED_PRECONDITION and pipeline:
            return PipelineError(detail or "pipeline rejected by switch")
        if code == grpc.StatusCode.INVALID_ARGUMENT:
            return P4RuntimeError(f"invalid argument: {detail}")
        return P4RuntimeError(f"gRPC error {code.name}: {detail}")

    # Context manager ----------------------------------------------------

    def __enter__(self) -> P4RuntimeClient:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.disconnect()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.disconnect()
