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
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

import grpc
from google.protobuf import text_format
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc

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
    ) -> None:
        self._target = target
        self._device_id = int(device_id)
        self._election_id_high = int(election_id[0])
        self._election_id_low = int(election_id[1])
        self._role_name = role
        self._channel_options: list[tuple[str, Any]] = (
            list(channel_options) if channel_options is not None else list(_DEFAULT_CHANNEL_OPTIONS)
        )

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

    # Properties ---------------------------------------------------------

    @property
    def target(self) -> str:
        return self._target

    @property
    def device_id(self) -> int:
        return self._device_id

    @property
    def election_id(self) -> tuple[int, int]:
        return (self._election_id_high, self._election_id_low)

    @property
    def index(self) -> P4InfoIndex:
        if self._index is None:
            raise P4RuntimeError(
                "no pipeline is set; call set_pipeline_config or get_pipeline_config first"
            )
        return self._index

    def is_connected(self) -> bool:
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
                # Future: handle packet/digest/etc.
        except grpc.RpcError as exc:
            if not self._closed:
                self._stream_error = exc
                self._stream_event.set()
        except Exception as exc:
            if not self._closed:
                self._stream_error = exc
                self._stream_event.set()

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
        """Return decoded entries for one table, or all tables if `table` is None."""
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
