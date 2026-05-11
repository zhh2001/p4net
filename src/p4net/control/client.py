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
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import grpc
from google.protobuf import text_format
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

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
from p4net.control.p4info_index import P4InfoIndex

logger = logging.getLogger(__name__)

_DEFAULT_CHANNEL_OPTIONS: tuple[tuple[str, Any], ...] = (
    ("grpc.max_send_message_length", 16 * 1024 * 1024),
    ("grpc.max_receive_message_length", 16 * 1024 * 1024),
    ("grpc.keepalive_time_ms", 10000),
)

_SENTINEL = object()
_PIPELINE_ACTIONS: frozenset[str] = frozenset(
    {"VERIFY", "VERIFY_AND_SAVE", "VERIFY_AND_COMMIT", "COMMIT", "RECONCILE_AND_COMMIT"}
)


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


def _extract_p4_canonical_codes(exc: grpc.RpcError) -> list[int]:
    """Pull per-update `p4.v1.Error.canonical_code` ints out of a gRPC error."""
    codes: list[int] = []
    try:
        metadata = exc.trailing_metadata() or ()
    except Exception:
        return codes
    for key, value in metadata:
        if key.lower() != "grpc-status-details-bin":
            continue
        try:
            from google.rpc import status_pb2

            rpc_status = status_pb2.Status()
            rpc_status.MergeFromString(value)
        except Exception:
            continue
        for detail in rpc_status.details:
            try:
                if detail.Is(p4runtime_pb2.Error.DESCRIPTOR):
                    err = p4runtime_pb2.Error()
                    detail.Unpack(err)
                    codes.append(int(err.canonical_code))
            except Exception:
                continue
    return codes


def _grpc_code_for(canonical: int) -> grpc.StatusCode | None:
    for sc in grpc.StatusCode:
        if sc.value[0] == canonical:
            return sc
    return None


class P4RuntimeClient:
    """gRPC client for a single P4Runtime device."""

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
        self._stream_event = threading.Event()
        self._arbitration: Any = None
        self._stream_error: BaseException | None = None
        self._connected = False
        self._closed = False
        self._index: P4InfoIndex | None = None
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
    def index(self) -> P4InfoIndex:
        """The :class:`P4InfoIndex` for the currently-pushed pipeline.

        Raises:
            P4RuntimeError: if no pipeline has been set yet.
        """
        if self._index is None:
            raise P4RuntimeError(
                "no pipeline is set; call set_pipeline_config or get_pipeline_config first"
            )
        return self._index

    def is_connected(self) -> bool:
        """``True`` while the gRPC channel is open and arbitration succeeded."""
        return self._connected and not self._closed

    # Lifecycle ----------------------------------------------------------

    def connect(self, *, timeout: float = 10.0) -> None:
        """Open the gRPC channel, start the StreamChannel, do master arbitration."""
        if self._connected:
            return
        self._closed = False
        self._channel = grpc.insecure_channel(self._target, options=self._channel_options)
        self._stub = p4runtime_pb2_grpc.P4RuntimeStub(self._channel)
        self._outgoing = queue.Queue()
        self._stream_event.clear()
        self._arbitration = None
        self._stream_error = None
        try:
            self._stream_call = self._stub.StreamChannel(self._request_generator())
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc) from exc
        self._stream_thread = threading.Thread(
            target=self._stream_consumer, name="p4rt-stream", daemon=True
        )
        self._stream_thread.start()

        # Send the initial arbitration update.
        req = p4runtime_pb2.StreamMessageRequest()
        req.arbitration.device_id = self._device_id
        req.arbitration.election_id.high = self._election_id_high
        req.arbitration.election_id.low = self._election_id_low
        if self._role_name:
            req.arbitration.role.name = self._role_name
        assert self._outgoing is not None
        self._outgoing.put(req)

        if not self._stream_event.wait(timeout):
            self._teardown()
            raise ConnectionError(
                f"P4Runtime arbitration timed out after {timeout}s for {self._target!r}"
            )
        if self._stream_error is not None:
            err = self._stream_error
            self._teardown()
            if isinstance(err, grpc.RpcError):
                raise self._translate_rpc_error(err) from err
            raise P4RuntimeError(f"stream error: {err!r}") from err
        if self._arbitration is None:
            self._teardown()
            raise ConnectionError("no arbitration response received")
        status_code = int(self._arbitration.status.code)
        if status_code != 0:
            self._teardown()
            raise NotPrimaryError(
                f"client is not primary for device {self._device_id} "
                f"(status code {status_code}, message {self._arbitration.status.message!r})"
            )
        self._connected = True
        logger.debug(
            "P4Runtime client %r connected, election_id=(%d, %d)",
            self._target,
            self._election_id_high,
            self._election_id_low,
        )

    def disconnect(self) -> None:
        """Close the stream channel and the gRPC channel. Idempotent."""
        if self._closed:
            return
        self._teardown()

    def _teardown(self) -> None:
        self._closed = True
        was_connected = self._connected
        self._connected = False
        if self._outgoing is not None:
            with contextlib.suppress(queue.Full):
                self._outgoing.put_nowait(_SENTINEL)
        if self._stream_call is not None:
            try:
                self._stream_call.cancel()
            except Exception as exc:
                logger.debug("stream cancel raised: %r", exc)
        if self._stream_thread is not None and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=2.0)
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
        if was_connected:
            logger.debug("P4Runtime client %r disconnected", self._target)

    def _request_generator(self) -> Iterator[Any]:
        assert self._outgoing is not None
        while True:
            msg = self._outgoing.get()
            if msg is _SENTINEL:
                return
            yield msg

    def _stream_consumer(self) -> None:
        try:
            assert self._stream_call is not None
            for resp in self._stream_call:
                if resp.HasField("arbitration"):
                    self._arbitration = resp.arbitration
                    self._stream_event.set()
                elif resp.HasField("packet"):
                    self._dispatch_packet_in(resp.packet)
                # Future: digest / idle-notification / etc.
        except grpc.RpcError as exc:
            if not self._closed:
                self._stream_error = exc
                self._stream_event.set()
        except Exception as exc:
            if not self._closed:
                self._stream_error = exc
                self._stream_event.set()

    def _dispatch_packet_in(self, packet: Any) -> None:
        """Decode a PacketIn and fan out to registered handlers."""
        payload = bytes(packet.payload)
        metadata: dict[str, int] = {}
        if self._index is not None:
            try:
                metadata = self._index.decode_packet_in_metadata(packet.metadata)
            except Exception as exc:
                logger.debug("decode_packet_in_metadata raised: %r", exc)
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
        bmv2_json: Path,
        p4info: Path,
        action: str = "VERIFY_AND_COMMIT",
        timeout: float = 10.0,
    ) -> None:
        """Push a compiled pipeline to the device."""
        self._require_connected()
        if action not in _PIPELINE_ACTIONS:
            raise P4RuntimeError(
                f"invalid pipeline action {action!r}; must be one of {sorted(_PIPELINE_ACTIONS)}"
            )
        p4info_msg = p4info_pb2.P4Info()
        text_format.Merge(Path(p4info).read_text(), p4info_msg)
        bmv2_data = Path(bmv2_json).read_bytes()

        req = p4runtime_pb2.SetForwardingPipelineConfigRequest()
        req.device_id = self._device_id
        if self._role_name:
            req.role = self._role_name
        req.election_id.high = self._election_id_high
        req.election_id.low = self._election_id_low
        req.action = p4runtime_pb2.SetForwardingPipelineConfigRequest.Action.Value(action)
        req.config.p4info.CopyFrom(p4info_msg)
        req.config.p4_device_config = bmv2_data

        try:
            self._stub.SetForwardingPipelineConfig(req, timeout=timeout)
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc, pipeline=True) from exc
        self._index = P4InfoIndex(p4info_msg)
        logger.debug(
            "P4Runtime pipeline pushed (action=%s, p4info_tables=%d)",
            action,
            len(p4info_msg.tables),
        )

    def get_pipeline_config(self, *, timeout: float = 10.0) -> tuple[bytes, P4InfoIndex]:
        """Read the current pipeline back from the device."""
        self._require_connected()
        req = p4runtime_pb2.GetForwardingPipelineConfigRequest()
        req.device_id = self._device_id
        req.response_type = p4runtime_pb2.GetForwardingPipelineConfigRequest.ResponseType.Value(
            "ALL"
        )
        try:
            resp = self._stub.GetForwardingPipelineConfig(req, timeout=timeout)
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc) from exc
        index = P4InfoIndex(resp.config.p4info)
        self._index = index
        return resp.config.p4_device_config, index

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
            priority: Required for tables with ternary or range keys.
            timeout: Per-call gRPC deadline in seconds.

        Raises:
            DuplicateEntryError: if an entry with the same key already exists.
            EncodingError: if any field can't be encoded.
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
        """Delete an entry from ``table`` by match key.

        Raises:
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
        self._require_connected_with_index()
        index = self._index
        assert index is not None
        table_id = index.table_id(table)
        requires_priority = index.table_requires_priority(table)
        if requires_priority and priority is None and update_type != "DELETE":
            raise EncodingError(
                f"table {table!r} has ternary/range match fields; priority is required"
            )
        if not requires_priority and priority is not None:
            raise EncodingError(f"table {table!r} is exact/lpm-only; priority must be None")
        field_matches = index.encode_match(table, match)
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = table_id
        for fm in field_matches:
            entry.match.add().CopyFrom(fm)
        if action is not None:
            entry.action.action.CopyFrom(index.encode_action(action, params))
        if priority is not None:
            entry.priority = int(priority)

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

        try:
            self._stub.Write(req, timeout=timeout)
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc) from exc
        logger.debug(
            "P4Runtime %s table_entry table=%s match=%s action=%s",
            update_type,
            table,
            dict(match),
            action,
        )

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
        """
        self._require_connected_with_index()
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self._device_id
        entity = req.entities.add()
        if table is not None:
            entity.table_entry.table_id = self.index.table_id(table)
        else:
            entity.table_entry.table_id = 0
        try:
            response_iter = self._stub.Read(req, timeout=timeout)
            entries: list[dict[str, Any]] = []
            for resp in response_iter:
                for ent in resp.entities:
                    if ent.HasField("table_entry"):
                        entries.append(self._decode_table_entry(ent.table_entry))
            return entries
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc) from exc

    def clear_table(self, table: str, *, timeout: float = 10.0) -> int:
        """Delete every entry from `table`. Returns the count deleted."""
        self._require_connected_with_index()
        entries = self.list_table_entries(table, timeout=timeout)
        if not entries:
            return 0
        index = self._index
        assert index is not None
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
        try:
            self._stub.Write(req, timeout=timeout)
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc) from exc
        return len(entries)

    def _decode_table_entry(self, entry: Any) -> dict[str, Any]:
        index = self._index
        assert index is not None
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
        """Read one or all populated cells of an indirect counter."""
        self._require_connected_with_index()
        idx = self._index
        assert idx is not None
        counter_id = idx.counter_id(counter)
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self._device_id
        entity = req.entities.add()
        entity.counter_entry.counter_id = counter_id
        if index is not None:
            entity.counter_entry.index.index = int(index)
        try:
            response_iter = self._stub.Read(req, timeout=timeout)
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
        if index is not None:
            return collected.get(int(index), CounterData(0, 0))
        return collected

    def reset_counter(
        self,
        counter: str,
        index: int | None = None,
        *,
        timeout: float = 5.0,
    ) -> None:
        """Zero one or all indices of an indirect counter."""
        self._require_connected_with_index()
        idx = self._index
        assert idx is not None
        counter_id = idx.counter_id(counter)
        targets: list[int]
        if index is None:
            current = self.read_counter(counter, timeout=timeout)
            assert isinstance(current, dict)
            targets = sorted(current.keys())
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
        try:
            self._stub.Write(req, timeout=timeout)
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc) from exc

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
            P4RuntimeError: if the underlying control channel returns
                an error or this client has no Thrift address configured.
        """
        self._require_connected_with_index()
        idx = self._index
        assert idx is not None
        spec = idx.register_by_name(name)
        if not 0 <= int(index) < spec.size:
            raise EncodingError(f"register {name!r} index {index} out of range [0, {spec.size})")
        # Force bitwidth validation. Result bytes are unused — the Thrift
        # CLI takes a decimal integer.
        encode_value(int(value), spec.bitwidth)
        self._run_thrift_cli(
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
            P4RuntimeError: if the underlying control channel returns
                an error or this client has no Thrift address configured.
        """
        self._require_connected_with_index()
        idx = self._index
        assert idx is not None
        spec = idx.register_by_name(name)
        if index is not None and not 0 <= int(index) < spec.size:
            raise EncodingError(f"register {name!r} index {index} out of range [0, {spec.size})")
        if index is not None:
            output = self._run_thrift_cli(
                f"register_read {name} {int(index)}",
                timeout=timeout,
                op_description=f"register_read {name}[{index}]",
            )
            return _parse_register_read_single(output, name, int(index))
        output = self._run_thrift_cli(
            f"register_read {name}",
            timeout=timeout,
            op_description=f"register_read {name}",
        )
        return _parse_register_read_array(output, name, spec.size)

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
        """Create a multicast group with one replica per port (instance=1)."""
        self._mcast_write(group_id, ports, update_type="INSERT", timeout=timeout)

    def modify_multicast_group(
        self,
        group_id: int,
        ports: Sequence[int],
        *,
        timeout: float = 5.0,
    ) -> None:
        """Replace the replica list of an existing multicast group."""
        self._mcast_write(group_id, ports, update_type="MODIFY", timeout=timeout)

    def delete_multicast_group(
        self,
        group_id: int,
        *,
        timeout: float = 5.0,
    ) -> None:
        """Delete a multicast group."""
        self._mcast_write(group_id, ports=(), update_type="DELETE", timeout=timeout)

    def _mcast_write(
        self,
        group_id: int,
        ports: Sequence[int],
        *,
        update_type: str,
        timeout: float,
    ) -> None:
        self._require_connected()
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
        try:
            self._stub.Write(req, timeout=timeout)
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc) from exc

    def list_multicast_groups(self, *, timeout: float = 5.0) -> dict[int, list[int]]:
        """Return ``{group_id: [egress_port, ...]}``.

        Replica instance numbers are flattened away — each port appears once
        per replica regardless of its instance value (we always write
        instance=1 ourselves; foreign instance values are still listed but
        not exposed in this dict shape).
        """
        self._require_connected()
        req = p4runtime_pb2.ReadRequest()
        req.device_id = self._device_id
        entity = req.entities.add()
        entity.packet_replication_engine_entry.multicast_group_entry.multicast_group_id = 0
        try:
            response_iter = self._stub.Read(req, timeout=timeout)
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
            return groups
        except grpc.RpcError as exc:
            raise self._translate_rpc_error(exc) from exc

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
        """
        self._require_connected()
        idx = self._index
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
        out = self._outgoing
        if out is None:
            raise ConnectionError("client is not connected; outgoing queue is closed")
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
        """Block until the next PacketIn arrives. Raises ``P4RuntimeError`` on timeout."""
        self._require_connected()
        q: queue.Queue[tuple[bytes, dict[str, int]]] = queue.Queue(maxsize=1)

        def _push(payload: bytes, meta: dict[str, int]) -> None:
            with contextlib.suppress(queue.Full):
                q.put_nowait((payload, meta))

        deregister = self.on_packet_in(_push)
        try:
            try:
                return q.get(timeout=timeout)
            except queue.Empty as exc:
                raise P4RuntimeError(f"no PacketIn within {timeout}s on {self._target!r}") from exc
        finally:
            deregister()

    # Internals ----------------------------------------------------------

    def _require_connected(self) -> None:
        if not self.is_connected():
            raise ConnectionError("client is not connected; call connect() first")

    def _require_connected_with_index(self) -> None:
        self._require_connected()
        if self._index is None:
            raise P4RuntimeError(
                "no pipeline is set; call set_pipeline_config or get_pipeline_config first"
            )

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
