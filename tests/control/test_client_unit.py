"""Unit tests for `p4net.control.client.P4RuntimeClient`. All gRPC mocked."""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import grpc
import pytest
from google.rpc import code_pb2, status_pb2
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
        self.read_started = threading.Event()

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
        self.read_started.set()
        item = self.responses.get()
        if item is StopIteration or self._cancelled:
            raise StopIteration
        if isinstance(item, BaseException):
            raise item
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
    t_opt = p.tables.add()
    t_opt.preamble.id = 2003
    t_opt.preamble.name = "MyIngress.optional_acl"
    mf_opt = t_opt.match_fields.add()
    mf_opt.id = 1
    mf_opt.name = "hdr.ethernet.etherType"
    mf_opt.bitwidth = 16
    mf_opt.match_type = p4info_pb2.MatchField.OPTIONAL
    t_opt.action_refs.add().id = 1001
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


@pytest.mark.parametrize("ending", ["eof", "rpc-error", "exception"])
def test_stream_end_releases_resources_and_allows_reconnect(
    patched_grpc: dict[str, Any], mocker: MockerFixture, ending: str
) -> None:
    stream: FakeStreamCall = patched_grpc["stream"]
    stream.responses.put(_make_arbitration_response(1, (1, 0)))
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    client.connect(timeout=2.0)
    consumer = client._stream_thread
    assert consumer is not None
    error = None
    if ending == "rpc-error":
        error = _grpc_error(grpc.StatusCode.UNAVAILABLE, "server stopped")
    elif ending == "exception":
        error = RuntimeError("stream failed")
    try:
        stream.responses.put(error if error is not None else StopIteration)
        consumer.join(timeout=2.0)
        assert not consumer.is_alive()
        assert not client.is_connected()
        patched_grpc["channel"].close.assert_called_once()
        assert stream._cancelled
        assert client._channel is None
        assert client._stub is None
        assert client._stream_call is None
        assert client._stream_thread is None
        assert client._outgoing is None
        if error is not None:
            assert client._stream_error is error
        with pytest.raises(ConnectionError):
            client.get_pipeline_config()

        replacement = FakeStreamCall()
        replacement.responses.put(_make_arbitration_response(1, (1, 0)))
        channel = MagicMock(name="replacement-channel")
        mocker.patch("p4net.control.client.grpc.insecure_channel", return_value=channel)

        def open_stream(requests: Iterator[Any]) -> FakeStreamCall:
            replacement.attach_request_iter(requests)
            return replacement

        patched_grpc["stub"].StreamChannel.side_effect = open_stream
        client.connect(timeout=2.0)
        assert client.is_connected()
        assert client._channel is channel
        assert client._stream_call is replacement
        assert client._stream_error is None
    finally:
        client.disconnect()


@pytest.mark.parametrize("arbitrated", [False, True])
def test_stream_end_during_connect_fails_promptly(
    patched_grpc: dict[str, Any], mocker: MockerFixture, arbitrated: bool
) -> None:
    stream: FakeStreamCall = patched_grpc["stream"]
    if arbitrated:
        stream.responses.put(_make_arbitration_response(1, (1, 0)))
    stream.responses.put(StopIteration)
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    if arbitrated:
        open_stream = patched_grpc["stub"].StreamChannel.side_effect

        def wait_for_consumer_before_returning(requests: Iterator[Any]) -> FakeStreamCall:
            event = client._stream_event
            wait = event.wait

            def wait_for_end(timeout: float | None = None) -> bool:
                signalled = wait(timeout)
                consumer = client._stream_thread
                if consumer is not None:
                    consumer.join(timeout=2.0)
                    assert not consumer.is_alive()
                return signalled

            mocker.patch.object(event, "wait", side_effect=wait_for_end)
            return open_stream(requests)

        patched_grpc["stub"].StreamChannel.side_effect = wait_for_consumer_before_returning
    try:
        with pytest.raises(ConnectionError, match="stream ended"):
            client.connect(timeout=2.0)
        assert not client.is_connected()
        patched_grpc["channel"].close.assert_called_once()
    finally:
        client.disconnect()


@pytest.mark.parametrize("rpc_error", [False, True])
def test_initial_stream_error_preserves_cause(
    patched_grpc: dict[str, Any], rpc_error: bool
) -> None:
    error = (
        _grpc_error(grpc.StatusCode.UNAVAILABLE, "server stopped")
        if rpc_error
        else RuntimeError("stream failed")
    )
    patched_grpc["stream"].responses.put(error)
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    try:
        with pytest.raises(ConnectionError if rpc_error else P4RuntimeError) as raised:
            client.connect(timeout=2.0)
        assert raised.value.__cause__ is error
        assert not client.is_connected()
        patched_grpc["channel"].close.assert_called_once()
    finally:
        client.disconnect()


@pytest.mark.parametrize("operation", ["disconnect", "reconnect"])
def test_lifecycle_waits_for_stream_cleanup(
    patched_grpc: dict[str, Any], mocker: MockerFixture, operation: str
) -> None:
    stream: FakeStreamCall = patched_grpc["stream"]
    stream.responses.put(_make_arbitration_response(1, (1, 0)))
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    client.connect(timeout=2.0)
    close_started = threading.Event()
    release_close = threading.Event()
    operation_started = threading.Event()

    def close() -> None:
        close_started.set()
        assert release_close.wait(timeout=5.0)

    patched_grpc["channel"].close.side_effect = close
    replacement = FakeStreamCall()
    replacement.responses.put(_make_arbitration_response(1, (1, 0)))
    channel = MagicMock(name="replacement-channel")

    def open_stream(requests: Iterator[Any]) -> FakeStreamCall:
        replacement.attach_request_iter(requests)
        return replacement

    def run_operation() -> None:
        operation_started.set()
        if operation == "reconnect":
            client.connect(timeout=2.0)
        else:
            client.disconnect()

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            stream.responses.put(StopIteration)
            assert close_started.wait(timeout=1.0)
            assert not client.is_connected()
            mocker.patch("p4net.control.client.grpc.insecure_channel", return_value=channel)
            patched_grpc["stub"].StreamChannel.side_effect = open_stream
            task = pool.submit(run_operation)
            assert operation_started.wait(timeout=1.0)
            assert not task.done()
            release_close.set()
            task.result(timeout=2.0)
            patched_grpc["channel"].close.assert_called_once()
            assert client.is_connected() == (operation == "reconnect")
            if operation == "reconnect":
                assert client._channel is channel
        finally:
            release_close.set()
            client.disconnect()


def test_disconnect_wakes_pending_connect(patched_grpc: dict[str, Any]) -> None:
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        attempt = pool.submit(client.connect, timeout=5.0)
        try:
            assert patched_grpc["stream"].read_started.wait(timeout=1.0)
            client.disconnect()
            with pytest.raises(ConnectionError, match="closed"):
                attempt.result(timeout=1.0)
            assert not client.is_connected()
        finally:
            client.disconnect()


def test_packet_handler_can_disconnect(patched_grpc: dict[str, Any]) -> None:
    stream: FakeStreamCall = patched_grpc["stream"]
    stream.responses.put(_make_arbitration_response(1, (1, 0)))
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    client.connect(timeout=2.0)
    handled = threading.Event()
    errors: list[Exception] = []

    def handler(payload: bytes, metadata: dict[str, int]) -> None:
        try:
            client.disconnect()
        except Exception as exc:
            errors.append(exc)
        finally:
            handled.set()

    client.on_packet_in(handler)
    packet = p4runtime_pb2.StreamMessageResponse()
    packet.packet.payload = b"test"
    try:
        stream.responses.put(packet)
        assert handled.wait(timeout=2.0)
        assert not errors
        assert not client.is_connected()
        patched_grpc["channel"].close.assert_called_once()
    finally:
        client._teardown()


@pytest.mark.parametrize("ending", ["eof", "exception"])
def test_old_consumer_does_not_close_reconnected_client(
    patched_grpc: dict[str, Any], mocker: MockerFixture, ending: str
) -> None:
    class DelayedStream(FakeStreamCall):
        def __next__(self) -> Any:
            try:
                return super().__next__()
            except StopIteration:
                if ending == "exception":
                    raise RuntimeError("old stream failed") from None
                raise

    stream = DelayedStream()
    replacement = FakeStreamCall()
    streams = iter([stream, replacement])

    def open_stream(requests: Iterator[Any]) -> FakeStreamCall:
        current = next(streams)
        current.attach_request_iter(requests)
        return current

    patched_grpc["stub"].StreamChannel.side_effect = open_stream
    stream.responses.put(_make_arbitration_response(1, (1, 0)))
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    client.connect(timeout=2.0)
    consumer = client._stream_thread
    assert consumer is not None
    entered_handler = threading.Event()
    release_handler = threading.Event()

    def handler(payload: bytes, metadata: dict[str, int]) -> None:
        entered_handler.set()
        assert release_handler.wait(timeout=5.0)

    client.on_packet_in(handler)
    packet = p4runtime_pb2.StreamMessageResponse()
    packet.packet.payload = b"old packet"
    try:
        stream.responses.put(packet)
        assert entered_handler.wait(timeout=1.0)
        # Model a callback that outlives disconnect()'s bounded join.
        with patch.object(consumer, "join", return_value=None):
            client.disconnect()
        channel = MagicMock(name="replacement-channel")
        mocker.patch("p4net.control.client.grpc.insecure_channel", return_value=channel)
        replacement.responses.put(_make_arbitration_response(1, (1, 0)))
        client.connect(timeout=2.0)
        release_handler.set()
        consumer.join(timeout=1.0)
        assert not consumer.is_alive()
        assert client.is_connected()
        assert client._channel is channel
        assert client._stream_call is replacement
        assert client._stream_error is None
        channel.close.assert_not_called()
        assert not replacement._cancelled
    finally:
        release_handler.set()
        client.disconnect()


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


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("operation", ["insert", "modify", "delete", "multicast", "pipeline"])
def test_write_permission_denied_raises_not_primary(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    wrapped: bool,
    operation: str,
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    error = _grpc_error(
        grpc.StatusCode.UNKNOWN if wrapped else grpc.StatusCode.PERMISSION_DENIED,
        "controller is not primary",
    )
    if wrapped:
        status = status_pb2.Status(code=code_pb2.UNKNOWN)
        status.details.add().Pack(p4runtime_pb2.Error(canonical_code=code_pb2.OK))
        status.details.add().Pack(p4runtime_pb2.Error(canonical_code=code_pb2.PERMISSION_DENIED))
        error.trailing_metadata = lambda: (  # type: ignore[method-assign]
            ("grpc-status-details-bin", status.SerializeToString()),
        )
    stub.Write = MagicMock(side_effect=error)
    stub.SetForwardingPipelineConfig = MagicMock(side_effect=error)
    index = client.index
    try:
        with pytest.raises(NotPrimaryError, match="controller is not primary") as raised:
            if operation == "pipeline":
                client.set_pipeline_config(
                    bmv2_json=connected_client[3], p4info=connected_client[2]
                )
            elif operation == "multicast":
                client.add_multicast_group(1, [1, 2])
            elif operation == "delete":
                client.delete_table_entry("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.1.0/24"})
            else:
                method = (
                    client.insert_table_entry
                    if operation == "insert"
                    else client.modify_table_entry
                )
                method(
                    "MyIngress.ipv4_lpm",
                    {"hdr.ipv4.dstAddr": "10.0.1.0/24"},
                    "MyIngress.set_egress_port",
                    {"port": 2},
                )
        assert raised.value.__cause__ is error
        assert client.is_connected()
        assert client.index is index
    finally:
        client.disconnect()


def test_write_permission_denied_without_detail(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    connected_client[1]["stub"].Write = MagicMock(
        side_effect=_grpc_error(grpc.StatusCode.PERMISSION_DENIED)
    )
    try:
        with pytest.raises(NotPrimaryError, match="not primary"):
            client.add_multicast_group(1, [1, 2])
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


@pytest.mark.parametrize("operation", ["insert", "modify", "delete"])
@pytest.mark.parametrize("wildcard", [False, True])
def test_optional_table_write_preserves_priority(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    operation: str,
    wildcard: bool,
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    match = {} if wildcard else {"hdr.ethernet.etherType": 0x0800}
    method = getattr(client, f"{operation}_table_entry")
    try:
        if operation == "delete":
            method("MyIngress.optional_acl", match, priority=17)
        else:
            method("MyIngress.optional_acl", match, "NoAction", priority=17)
        stub.Write.assert_called_once()
        request = stub.Write.call_args.args[0]
        update = request.updates[0]
        assert update.type == p4runtime_pb2.Update.Type.Value(operation.upper())
        entry = update.entity.table_entry
        assert entry.table_id == 2003
        assert entry.priority == 17
        assert entry.HasField("action") == (operation != "delete")
        if wildcard:
            assert len(entry.match) == 0
        else:
            (field,) = entry.match
            assert field.field_id == 1
            assert field.HasField("optional")
            assert field.optional.value == b"\x08\x00"
    finally:
        client.disconnect()


@pytest.mark.parametrize("operation", ["insert", "modify"])
@pytest.mark.parametrize("wildcard", [False, True])
def test_optional_table_missing_priority_does_not_write(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    operation: str,
    wildcard: bool,
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    method = getattr(client, f"{operation}_table_entry")
    match = {} if wildcard else {"hdr.ethernet.etherType": 0x0800}
    try:
        with pytest.raises(EncodingError, match=r"optional.*priority is required"):
            method("MyIngress.optional_acl", match, "NoAction")
        stub.Write.assert_not_called()
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
