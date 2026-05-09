"""Unit tests for `p4net.control.client.P4RuntimeClient`. All gRPC mocked."""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2
from pytest_mock import MockerFixture

import p4net.control  # ensures protobuf python-impl env var is set  # noqa: F401
from p4net.control import (
    ConnectionError,
    DuplicateEntryError,
    EncodingError,
    EntryNotFoundError,
    NotPrimaryError,
    P4RuntimeClient,
    P4RuntimeError,
    PipelineError,
)

# ---------------------------------------------------------------------------
# Helpers: a fake StreamChannel that drains the request iterator on a thread
# and yields prepared responses from a queue.
# ---------------------------------------------------------------------------


class FakeStreamCall:
    """Simulates the bidirectional gRPC stream returned by `stub.StreamChannel`."""

    def __init__(self) -> None:
        self.responses: queue.Queue[Any] = queue.Queue()
        self.requests_seen: list[Any] = []
        self._cancelled = False
        self._reader: threading.Thread | None = None
        self._request_iter: Iterator[Any] | None = None

    def attach_request_iter(self, request_iter: Iterator[Any]) -> None:
        self._request_iter = request_iter
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        try:
            assert self._request_iter is not None
            for req in self._request_iter:
                self.requests_seen.append(req)
        except Exception:
            pass

    def __iter__(self) -> FakeStreamCall:
        return self

    def __next__(self) -> Any:
        item = self.responses.get()
        if item is StopIteration or self._cancelled:
            raise StopIteration
        return item

    def cancel(self) -> None:
        self._cancelled = True
        self.responses.put(StopIteration)


def _make_arbitration_response(device_id: int, election_id: tuple[int, int], code: int = 0) -> Any:
    resp = p4runtime_pb2.StreamMessageResponse()
    resp.arbitration.device_id = device_id
    resp.arbitration.election_id.high = election_id[0]
    resp.arbitration.election_id.low = election_id[1]
    resp.arbitration.status.code = code
    return resp


def _build_p4info() -> p4info_pb2.P4Info:
    p = p4info_pb2.P4Info()
    a_no = p.actions.add()
    a_no.preamble.id = 1001
    a_no.preamble.name = "NoAction"
    a_set = p.actions.add()
    a_set.preamble.id = 1002
    a_set.preamble.name = "MyIngress.set_egress_port"
    pa = a_set.params.add()
    pa.id = 1
    pa.name = "port"
    pa.bitwidth = 9
    t_lpm = p.tables.add()
    t_lpm.preamble.id = 2001
    t_lpm.preamble.name = "MyIngress.ipv4_lpm"
    mf = t_lpm.match_fields.add()
    mf.id = 1
    mf.name = "hdr.ipv4.dstAddr"
    mf.bitwidth = 32
    mf.match_type = p4info_pb2.MatchField.LPM
    t_lpm.action_refs.add().id = 1001
    t_lpm.action_refs.add().id = 1002
    t_tern = p.tables.add()
    t_tern.preamble.id = 2002
    t_tern.preamble.name = "MyIngress.ternary_acl"
    mft = t_tern.match_fields.add()
    mft.id = 1
    mft.name = "hdr.ipv4.dstAddr"
    mft.bitwidth = 32
    mft.match_type = p4info_pb2.MatchField.TERNARY
    t_tern.action_refs.add().id = 1001
    return p


@pytest.fixture
def patched_grpc(mocker: MockerFixture) -> dict[str, Any]:
    chan = MagicMock(name="grpc-channel")
    stub = MagicMock(name="P4RuntimeStub")
    stream = FakeStreamCall()

    def stream_channel(request_iter: Iterator[Any]) -> FakeStreamCall:
        stream.attach_request_iter(request_iter)
        return stream

    stub.StreamChannel = MagicMock(side_effect=stream_channel)
    mocker.patch("p4net.control.client.grpc.insecure_channel", return_value=chan)
    mocker.patch(
        "p4net.control.client.p4runtime_pb2_grpc.P4RuntimeStub",
        return_value=stub,
    )
    return {"channel": chan, "stub": stub, "stream": stream}


def _grpc_error(code: grpc.StatusCode, detail: str = "") -> grpc.RpcError:
    err = grpc.RpcError()

    def _code(_self: grpc.RpcError = err) -> grpc.StatusCode:
        return code

    def _details(_self: grpc.RpcError = err) -> str:
        return detail

    err.code = _code  # type: ignore[method-assign]
    err.details = _details  # type: ignore[method-assign]
    return err


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------


def test_connect_sends_arbitration_and_succeeds(
    patched_grpc: dict[str, Any],
) -> None:
    stream: FakeStreamCall = patched_grpc["stream"]
    client = P4RuntimeClient("127.0.0.1:50051", device_id=7, election_id=(10, 5))
    # Pre-load a successful arbitration response.
    stream.responses.put(_make_arbitration_response(7, (10, 5), code=0))
    client.connect(timeout=2.0)
    try:
        assert client.is_connected()
        # Verify the arbitration request that was sent.
        # Wait up to a moment for the request iterator to drain it.
        for _ in range(20):
            if stream.requests_seen:
                break
            threading.Event().wait(0.05)
        assert stream.requests_seen, "no arbitration request was sent"
        sent = stream.requests_seen[0]
        assert sent.arbitration.device_id == 7
        assert sent.arbitration.election_id.high == 10
        assert sent.arbitration.election_id.low == 5
    finally:
        client.disconnect()


def test_connect_with_role_includes_role_name(
    patched_grpc: dict[str, Any],
) -> None:
    stream: FakeStreamCall = patched_grpc["stream"]
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1, role="my-role")
    stream.responses.put(_make_arbitration_response(1, (1, 0), code=0))
    client.connect(timeout=2.0)
    try:
        for _ in range(20):
            if stream.requests_seen:
                break
            threading.Event().wait(0.05)
        assert stream.requests_seen[0].arbitration.role.name == "my-role"
    finally:
        client.disconnect()


def test_connect_raises_not_primary_on_nonzero_status(
    patched_grpc: dict[str, Any],
) -> None:
    stream: FakeStreamCall = patched_grpc["stream"]
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    stream.responses.put(_make_arbitration_response(1, (1, 0), code=6))  # ALREADY_EXISTS-ish
    with pytest.raises(NotPrimaryError, match="not primary"):
        client.connect(timeout=2.0)
    assert not client.is_connected()


def test_connect_raises_connection_error_on_unavailable(
    mocker: MockerFixture, patched_grpc: dict[str, Any]
) -> None:
    stub: MagicMock = patched_grpc["stub"]
    err = _grpc_error(grpc.StatusCode.UNAVAILABLE, "down")
    stub.StreamChannel = MagicMock(side_effect=err)
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    with pytest.raises(ConnectionError, match="unavailable"):
        client.connect(timeout=1.0)


def test_connect_arbitration_timeout(patched_grpc: dict[str, Any]) -> None:
    # Don't enqueue any responses; consumer will block forever on get().
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    with pytest.raises(ConnectionError, match="timed out"):
        client.connect(timeout=0.2)
    assert not client.is_connected()


def test_disconnect_is_idempotent(patched_grpc: dict[str, Any]) -> None:
    stream: FakeStreamCall = patched_grpc["stream"]
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    stream.responses.put(_make_arbitration_response(1, (1, 0), code=0))
    client.connect(timeout=2.0)
    client.disconnect()
    client.disconnect()
    client.disconnect()
    assert not client.is_connected()


def test_context_manager_calls_connect_and_disconnect(
    patched_grpc: dict[str, Any],
) -> None:
    stream: FakeStreamCall = patched_grpc["stream"]
    stream.responses.put(_make_arbitration_response(1, (1, 0), code=0))
    with P4RuntimeClient("127.0.0.1:50051", device_id=1) as client:
        assert client.is_connected()
    assert not client.is_connected()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@pytest.fixture
def connected_client(
    patched_grpc: dict[str, Any], tmp_path: Path
) -> tuple[P4RuntimeClient, dict[str, Any], Path, Path]:
    stream: FakeStreamCall = patched_grpc["stream"]
    stream.responses.put(_make_arbitration_response(0, (1, 0), code=0))
    client = P4RuntimeClient("127.0.0.1:50051", device_id=0)
    client.connect(timeout=2.0)
    p4info = _build_p4info()
    p4info_path = tmp_path / "p.p4info.txtpb"
    from google.protobuf import text_format

    p4info_path.write_text(text_format.MessageToString(p4info))
    json_path = tmp_path / "p.json"
    json_path.write_bytes(b'{"fake":"json"}')
    return client, patched_grpc, p4info_path, json_path


def test_set_pipeline_config_builds_request(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client, grpc_mocks, p4info_path, json_path = connected_client
    stub: MagicMock = grpc_mocks["stub"]
    stub.SetForwardingPipelineConfig = MagicMock(return_value=MagicMock())
    try:
        client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path, timeout=2.0)
        sent = stub.SetForwardingPipelineConfig.call_args.args[0]
        assert sent.device_id == 0
        assert sent.action == p4runtime_pb2.SetForwardingPipelineConfigRequest.Action.Value(
            "VERIFY_AND_COMMIT"
        )
        assert sent.config.p4_device_config == b'{"fake":"json"}'
        # Index is now populated.
        assert "MyIngress.ipv4_lpm" in client.index.table_names
    finally:
        client.disconnect()


def test_set_pipeline_config_failed_precondition_raises_pipeline_error(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client, grpc_mocks, p4info_path, json_path = connected_client
    stub: MagicMock = grpc_mocks["stub"]
    stub.SetForwardingPipelineConfig = MagicMock(
        side_effect=_grpc_error(grpc.StatusCode.FAILED_PRECONDITION, "bad pipeline")
    )
    try:
        with pytest.raises(PipelineError, match="bad pipeline"):
            client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path, timeout=2.0)
    finally:
        client.disconnect()


def test_set_pipeline_config_invalid_action_string(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client, _, p4info_path, json_path = connected_client
    try:
        with pytest.raises(P4RuntimeError, match="invalid pipeline action"):
            client.set_pipeline_config(
                bmv2_json=json_path,
                p4info=p4info_path,
                action="NOPE",
                timeout=2.0,
            )
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# Table CRUD
# ---------------------------------------------------------------------------


def _push_pipeline(
    bundle: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> P4RuntimeClient:
    client, grpc_mocks, p4info_path, json_path = bundle
    grpc_mocks["stub"].SetForwardingPipelineConfig = MagicMock(return_value=MagicMock())
    client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path, timeout=2.0)
    return client


def test_insert_table_entry_builds_write_request(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    stub.Write = MagicMock(return_value=MagicMock())
    try:
        client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.1.0/24"},
            "MyIngress.set_egress_port",
            {"port": 2},
        )
        req = stub.Write.call_args.args[0]
        assert req.device_id == 0
        assert len(req.updates) == 1
        upd = req.updates[0]
        assert upd.type == p4runtime_pb2.Update.Type.Value("INSERT")
        te = upd.entity.table_entry
        assert te.table_id == 2001
        assert len(te.match) == 1
        assert te.action.action.action_id == 1002
    finally:
        client.disconnect()


def test_insert_duplicate_raises_duplicate_entry_error(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    stub.Write = MagicMock(side_effect=_grpc_error(grpc.StatusCode.ALREADY_EXISTS, "exists"))
    try:
        with pytest.raises(DuplicateEntryError):
            client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": "10.0.1.0/24"},
                "MyIngress.set_egress_port",
                {"port": 2},
            )
    finally:
        client.disconnect()


def test_modify_table_entry_uses_modify_update(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    stub.Write = MagicMock(return_value=MagicMock())
    try:
        client.modify_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.1.0/24"},
            "MyIngress.set_egress_port",
            {"port": 3},
        )
        req = stub.Write.call_args.args[0]
        assert req.updates[0].type == p4runtime_pb2.Update.Type.Value("MODIFY")
    finally:
        client.disconnect()


def test_delete_table_entry_uses_delete_update(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    stub.Write = MagicMock(return_value=MagicMock())
    try:
        client.delete_table_entry("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.1.0/24"})
        req = stub.Write.call_args.args[0]
        assert req.updates[0].type == p4runtime_pb2.Update.Type.Value("DELETE")
        # No action proto on delete.
        assert not req.updates[0].entity.table_entry.HasField("action")
    finally:
        client.disconnect()


def test_delete_not_found_raises_entry_not_found_error(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    stub.Write = MagicMock(side_effect=_grpc_error(grpc.StatusCode.NOT_FOUND, "missing"))
    try:
        with pytest.raises(EntryNotFoundError):
            client.delete_table_entry("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.1.0/24"})
    finally:
        client.disconnect()


def test_priority_required_for_ternary_table(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    stub.Write = MagicMock(return_value=MagicMock())
    try:
        with pytest.raises(EncodingError, match="priority is required"):
            client.insert_table_entry(
                "MyIngress.ternary_acl",
                {"hdr.ipv4.dstAddr": ("10.0.0.0", "255.255.0.0")},
                "NoAction",
            )
        # With priority, insert succeeds.
        client.insert_table_entry(
            "MyIngress.ternary_acl",
            {"hdr.ipv4.dstAddr": ("10.0.0.0", "255.255.0.0")},
            "NoAction",
            priority=10,
        )
        assert stub.Write.called
    finally:
        client.disconnect()


def test_priority_forbidden_for_lpm_table(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    try:
        with pytest.raises(EncodingError, match="priority must be None"):
            client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": "10.0.1.0/24"},
                "MyIngress.set_egress_port",
                {"port": 2},
                priority=5,
            )
    finally:
        client.disconnect()


def test_list_table_entries_decodes_response(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]

    # Build a fake ReadResponse stream: one TableEntry that we encode here.
    entry = p4runtime_pb2.TableEntry()
    entry.table_id = 2001
    fm = entry.match.add()
    fm.field_id = 1
    fm.lpm.value = b"\x0a\x00\x01\x00"
    fm.lpm.prefix_len = 24
    entry.action.action.action_id = 1002
    ap = entry.action.action.params.add()
    ap.param_id = 1
    ap.value = b"\x02"
    resp = p4runtime_pb2.ReadResponse()
    e = resp.entities.add()
    e.table_entry.CopyFrom(entry)
    stub.Read = MagicMock(return_value=iter([resp]))
    try:
        decoded = client.list_table_entries("MyIngress.ipv4_lpm")
        assert len(decoded) == 1
        d = decoded[0]
        assert d["table"] == "MyIngress.ipv4_lpm"
        assert d["action"] == "MyIngress.set_egress_port"
        assert d["params"]["port"] == b"\x02"
        assert d["match"]["hdr.ipv4.dstAddr"] == (b"\x0a\x00\x01\x00", 24)
    finally:
        client.disconnect()


def test_operations_require_connect_first() -> None:
    client = P4RuntimeClient("127.0.0.1:50051", device_id=0)
    with pytest.raises(ConnectionError, match="not connected"):
        client.list_table_entries("ipv4_lpm")


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


def _build_p4info_with_counter() -> p4info_pb2.P4Info:
    p = _build_p4info()
    c = p.counters.add()
    c.preamble.id = 3001
    c.preamble.name = "MyIngress.ingress_pkts"
    return p


@pytest.fixture
def connected_client_with_counter(
    patched_grpc: dict[str, Any], tmp_path: Path
) -> tuple[P4RuntimeClient, dict[str, Any]]:
    stream: FakeStreamCall = patched_grpc["stream"]
    stream.responses.put(_make_arbitration_response(0, (1, 0), code=0))
    client = P4RuntimeClient("127.0.0.1:50051", device_id=0)
    client.connect(timeout=2.0)
    p4info = _build_p4info_with_counter()
    p4info_path = tmp_path / "p.p4info.txtpb"
    from google.protobuf import text_format

    p4info_path.write_text(text_format.MessageToString(p4info))
    json_path = tmp_path / "p.json"
    json_path.write_bytes(b"{}")
    patched_grpc["stub"].SetForwardingPipelineConfig = MagicMock(return_value=MagicMock())
    client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path, timeout=2.0)
    return client, patched_grpc


def test_read_counter_single_index(
    connected_client_with_counter: tuple[P4RuntimeClient, dict[str, Any]],
) -> None:
    from p4net.control import CounterData

    client, grpc_mocks = connected_client_with_counter
    stub: MagicMock = grpc_mocks["stub"]
    resp = p4runtime_pb2.ReadResponse()
    e = resp.entities.add()
    e.counter_entry.counter_id = 3001
    e.counter_entry.index.index = 0
    e.counter_entry.data.packet_count = 7
    e.counter_entry.data.byte_count = 700
    stub.Read = MagicMock(return_value=iter([resp]))
    try:
        result = client.read_counter("MyIngress.ingress_pkts", 0)
        assert result == CounterData(7, 700)
        sent = stub.Read.call_args.args[0]
        assert sent.entities[0].counter_entry.counter_id == 3001
        assert sent.entities[0].counter_entry.index.index == 0
    finally:
        client.disconnect()


def test_read_counter_all_indices(
    connected_client_with_counter: tuple[P4RuntimeClient, dict[str, Any]],
) -> None:
    from p4net.control import CounterData

    client, grpc_mocks = connected_client_with_counter
    stub: MagicMock = grpc_mocks["stub"]
    resp = p4runtime_pb2.ReadResponse()
    for i, (pkt, byt) in enumerate([(1, 64), (3, 192)]):
        e = resp.entities.add()
        e.counter_entry.counter_id = 3001
        e.counter_entry.index.index = i
        e.counter_entry.data.packet_count = pkt
        e.counter_entry.data.byte_count = byt
    stub.Read = MagicMock(return_value=iter([resp]))
    try:
        result = client.read_counter("MyIngress.ingress_pkts")
        assert isinstance(result, dict)
        assert result == {0: CounterData(1, 64), 1: CounterData(3, 192)}
    finally:
        client.disconnect()


def test_read_counter_missing_index_returns_zero(
    connected_client_with_counter: tuple[P4RuntimeClient, dict[str, Any]],
) -> None:
    from p4net.control import CounterData

    client, grpc_mocks = connected_client_with_counter
    stub: MagicMock = grpc_mocks["stub"]
    stub.Read = MagicMock(return_value=iter([p4runtime_pb2.ReadResponse()]))
    try:
        result = client.read_counter("MyIngress.ingress_pkts", 5)
        assert result == CounterData(0, 0)
    finally:
        client.disconnect()


def test_reset_counter_single_index_writes_zero(
    connected_client_with_counter: tuple[P4RuntimeClient, dict[str, Any]],
) -> None:
    client, grpc_mocks = connected_client_with_counter
    stub: MagicMock = grpc_mocks["stub"]
    stub.Write = MagicMock(return_value=MagicMock())
    try:
        client.reset_counter("MyIngress.ingress_pkts", 4)
        req = stub.Write.call_args.args[0]
        assert len(req.updates) == 1
        upd = req.updates[0]
        assert upd.type == p4runtime_pb2.Update.Type.Value("MODIFY")
        ce = upd.entity.counter_entry
        assert ce.counter_id == 3001
        assert ce.index.index == 4
        assert ce.data.packet_count == 0
        assert ce.data.byte_count == 0
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# Multicast
# ---------------------------------------------------------------------------


def test_add_multicast_group_builds_insert(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    stub.Write = MagicMock(return_value=MagicMock())
    try:
        client.add_multicast_group(7, [1, 2, 3])
        req = stub.Write.call_args.args[0]
        assert len(req.updates) == 1
        upd = req.updates[0]
        assert upd.type == p4runtime_pb2.Update.Type.Value("INSERT")
        mge = upd.entity.packet_replication_engine_entry.multicast_group_entry
        assert mge.multicast_group_id == 7
        ports = [(r.egress_port, r.instance) for r in mge.replicas]
        assert ports == [(1, 1), (2, 1), (3, 1)]
    finally:
        client.disconnect()


def test_modify_multicast_group_builds_modify(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    stub.Write = MagicMock(return_value=MagicMock())
    try:
        client.modify_multicast_group(7, [4, 5])
        req = stub.Write.call_args.args[0]
        assert req.updates[0].type == p4runtime_pb2.Update.Type.Value("MODIFY")
    finally:
        client.disconnect()


def test_delete_multicast_group_no_replicas(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    stub.Write = MagicMock(return_value=MagicMock())
    try:
        client.delete_multicast_group(7)
        req = stub.Write.call_args.args[0]
        upd = req.updates[0]
        assert upd.type == p4runtime_pb2.Update.Type.Value("DELETE")
        mge = upd.entity.packet_replication_engine_entry.multicast_group_entry
        assert mge.multicast_group_id == 7
        assert len(mge.replicas) == 0
    finally:
        client.disconnect()


def test_list_multicast_groups_decodes_response(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    resp = p4runtime_pb2.ReadResponse()
    e = resp.entities.add()
    mge = e.packet_replication_engine_entry.multicast_group_entry
    mge.multicast_group_id = 7
    for port in (1, 2, 3):
        r = mge.replicas.add()
        r.egress_port = port
        r.instance = 1
    e2 = resp.entities.add()
    mge2 = e2.packet_replication_engine_entry.multicast_group_entry
    mge2.multicast_group_id = 8
    r2 = mge2.replicas.add()
    r2.egress_port = 9
    r2.instance = 1
    stub.Read = MagicMock(return_value=iter([resp]))
    try:
        groups = client.list_multicast_groups()
        assert groups == {7: [1, 2, 3], 8: [9]}
    finally:
        client.disconnect()


def test_add_multicast_group_rejects_zero_id(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    try:
        with pytest.raises(EncodingError, match="must be positive"):
            client.add_multicast_group(0, [1])
    finally:
        client.disconnect()
