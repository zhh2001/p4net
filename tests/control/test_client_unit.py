"""Unit tests for `p4net.control.client.P4RuntimeClient`. All gRPC mocked."""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
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
    channel_factory = mocker.patch("p4net.control.client.grpc.insecure_channel", return_value=chan)
    stub_factory = mocker.patch(
        "p4net.control.client.p4runtime_pb2_grpc.P4RuntimeStub",
        return_value=stub,
    )
    return {
        "channel": chan,
        "channel_factory": channel_factory,
        "stub": stub,
        "stub_factory": stub_factory,
        "stream": stream,
    }


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


@pytest.mark.parametrize("step", ["stub", "stream"])
def test_connect_initialization_failure_closes_channel_and_allows_retry(
    patched_grpc: dict[str, Any], step: str
) -> None:
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    failed_channel = patched_grpc["channel"]
    retry_channel = MagicMock(name="retry-channel")
    patched_grpc["channel_factory"].side_effect = [failed_channel, retry_channel]
    failing_call = (
        patched_grpc["stub_factory"] if step == "stub" else patched_grpc["stub"].StreamChannel
    )
    original_side_effect = failing_call.side_effect
    failure = RuntimeError(f"{step} initialization failed")
    failing_call.side_effect = failure

    try:
        with pytest.raises(RuntimeError) as raised, client:
            pytest.fail("connection unexpectedly succeeded")
        assert raised.value is failure
        failed_channel.close.assert_called_once()
        assert client._channel is None
        assert client._stream_thread is None
        assert not client.is_connected()

        failing_call.side_effect = original_side_effect
        patched_grpc["stream"].responses.put(_make_arbitration_response(1, (1, 0)))
        with client:
            assert client.is_connected()
        retry_channel.close.assert_called_once()
        failed_channel.close.assert_called_once()
    finally:
        client.disconnect()


def test_connect_invalid_arbitration_request_releases_resources(
    patched_grpc: dict[str, Any],
) -> None:
    client = P4RuntimeClient("127.0.0.1:50051", device_id=-1)
    consumers: list[threading.Thread | None] = []
    patched_grpc["channel"].close.side_effect = lambda: consumers.append(client._stream_thread)
    try:
        with pytest.raises(ValueError, match="out of range"):
            client.connect()
        assert not client.is_connected()
        assert patched_grpc["stream"]._cancelled
        patched_grpc["channel"].close.assert_called_once()
        assert client._channel is None
        assert client._stream_thread is None
        (consumer,) = consumers
        assert consumer is not None
        assert not consumer.is_alive()
    finally:
        client.disconnect()


@pytest.mark.parametrize("outcome", ["success", "stream-error", "disconnect", "timeout"])
def test_concurrent_connects_share_one_attempt(
    patched_grpc: dict[str, Any], mocker: MockerFixture, outcome: str
) -> None:
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    stream = patched_grpc["stream"]
    replacement = FakeStreamCall()
    retry_channel = MagicMock(name="retry-channel")
    channel_factory = patched_grpc["channel_factory"]
    channel_factory.side_effect = [patched_grpc["channel"], retry_channel]
    waiter_started = threading.Event()
    fail_timeout = threading.Event()
    result = Future.result

    def wait_for_result(attempt: Future[None], timeout: float | None = None) -> None:
        if attempt is getattr(client, "_connect_attempt", None):
            waiter_started.set()
        return result(attempt, timeout)

    mocker.patch.object(Future, "result", new=wait_for_result)
    streams = iter([stream, replacement])

    def open_stream(requests: Iterator[Any]) -> FakeStreamCall:
        current = next(streams)
        if current is not stream:
            waiter_started.set()
        elif outcome == "timeout":

            def wait_for_timeout(timeout: float | None = None) -> bool:
                assert timeout == 3.0
                assert fail_timeout.wait(3.0)
                return False

            mocker.patch.object(client._stream_event, "wait", new=wait_for_timeout)
        current.attach_request_iter(requests)
        return current

    patched_grpc["stub"].StreamChannel.side_effect = open_stream
    with ThreadPoolExecutor(max_workers=2) as workers:
        try:
            first = workers.submit(client.connect, timeout=3.0)
            assert stream.read_started.wait(1.0)
            second = workers.submit(client.connect, timeout=0.0)
            assert waiter_started.wait(1.0)
            channel_factory.assert_called_once()
            assert not stream._cancelled

            if outcome == "success":
                stream.responses.put(_make_arbitration_response(1, (1, 0)))
                first.result(2.0)
                second.result(2.0)
            else:
                failure = _grpc_error(grpc.StatusCode.UNAVAILABLE, "server stopped")
                if outcome == "stream-error":
                    stream.responses.put(failure)
                elif outcome == "disconnect":
                    client.disconnect()
                else:
                    fail_timeout.set()

                for call in (first, second):
                    error = call.exception(timeout=2.0)
                    assert isinstance(error, ConnectionError)
                    if outcome == "stream-error":
                        assert error.__cause__ is failure
                    elif outcome == "disconnect":
                        assert "closed" in str(error)
                    else:
                        assert "timed out after 3.0s" in str(error)
                assert not client.is_connected()
                patched_grpc["channel"].close.assert_called_once()

                replacement.responses.put(_make_arbitration_response(1, (1, 0)))
                client.connect(timeout=2.0)
                assert channel_factory.call_count == 2
                assert client._channel is retry_channel
            assert client.is_connected()
        finally:
            fail_timeout.set()
            client.disconnect()


def test_packet_handler_cannot_block_its_own_arbitration(patched_grpc: dict[str, Any]) -> None:
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    stream = patched_grpc["stream"]
    errors: queue.Queue[ConnectionError] = queue.Queue()

    def connect_from_handler(_payload: bytes, _metadata: dict[str, int]) -> None:
        try:
            client.connect()
        except ConnectionError as error:
            errors.put(error)

    client.on_packet_in(connect_from_handler)
    packet = p4runtime_pb2.StreamMessageResponse()
    packet.packet.payload = b"test"
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(client.connect, timeout=3.0)
        try:
            assert stream.read_started.wait(1.0)
            stream.responses.put(packet)
            assert "PacketIn handler" in str(errors.get(timeout=1.0))
            stream.responses.put(_make_arbitration_response(1, (1, 0)))
            pending.result(timeout=2.0)
            patched_grpc["channel_factory"].assert_called_once()
            assert client.is_connected()
        finally:
            client.disconnect()


def test_retired_client_cannot_finish_pending_arbitration(patched_grpc: dict[str, Any]) -> None:
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    stream = patched_grpc["stream"]
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(client.connect, timeout=3.0)
        try:
            assert stream.read_started.wait(timeout=1.0)
            client._retire()
            stream.responses.put(_make_arbitration_response(1, (1, 0)))
            with pytest.raises(ConnectionError, match="network shutdown"):
                pending.result(timeout=2.0)
            assert not client.is_connected()
            assert client._stream_thread is None
            patched_grpc["channel"].close.assert_called_once()
        finally:
            client.disconnect()


@pytest.mark.parametrize(
    ("channel_options", "expected_options"),
    [
        pytest.param(
            None,
            [
                ("grpc.max_send_message_length", 16 * 1024 * 1024),
                ("grpc.max_receive_message_length", 16 * 1024 * 1024),
            ],
            id="defaults",
        ),
        pytest.param(
            [("grpc.keepalive_time_ms", 300_000)],
            [("grpc.keepalive_time_ms", 300_000)],
            id="explicit-keepalive",
        ),
    ],
)
def test_connect_uses_grpc_keepalive_only_when_configured(
    patched_grpc: dict[str, Any],
    channel_options: list[tuple[str, int]] | None,
    expected_options: list[tuple[str, int]],
) -> None:
    target = "127.0.0.1:50051"
    stream: FakeStreamCall = patched_grpc["stream"]
    stream.responses.put(_make_arbitration_response(1, (1, 0)))
    with P4RuntimeClient(target, device_id=1, channel_options=channel_options):
        patched_grpc["channel_factory"].assert_called_once_with(target, options=expected_options)


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
    with pytest.raises(ConnectionError, match="unavailable") as raised:
        client.connect(timeout=1.0)
    assert raised.value.__cause__ is err
    patched_grpc["channel"].close.assert_called_once()


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


@pytest.mark.parametrize("ending", ["disconnect", "eof", "rpc-error"])
def test_connection_close_wakes_packet_in_waiters(
    patched_grpc: dict[str, Any], mocker: MockerFixture, ending: str
) -> None:
    client = P4RuntimeClient("127.0.0.1:50051", device_id=1)
    stream = patched_grpc["stream"]
    stream.responses.put(_make_arbitration_response(1, (1, 0)))
    client.connect(timeout=2.0)
    consumer = client._stream_thread
    assert consumer is not None
    registrations: queue.Queue[None] = queue.Queue()
    register = client.on_packet_in

    def signal_registration(handler: Any) -> Any:
        unsubscribe = register(handler)
        registrations.put_nowait(None)
        return unsubscribe

    mocker.patch.object(client, "on_packet_in", side_effect=signal_registration)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            waiters = [pool.submit(client.expect_packet_in, timeout=2.0) for _ in range(2)]
            for _ in waiters:
                registrations.get(timeout=1.0)
            error = _grpc_error(grpc.StatusCode.UNAVAILABLE, "server stopped")
            if ending == "disconnect":
                client.disconnect()
            else:
                stream.responses.put(error if ending == "rpc-error" else StopIteration)

            for waiter in waiters:
                with pytest.raises(ConnectionError, match="closed while waiting") as result:
                    waiter.result(timeout=0.5)
                if ending == "rpc-error":
                    assert result.value.__cause__ is error
            assert client._packet_in_handlers == []
            consumer.join(timeout=1.0)
            assert not consumer.is_alive()
            patched_grpc["channel"].close.assert_called_once()

            replacement = FakeStreamCall()

            def open_stream(requests: Iterator[Any]) -> FakeStreamCall:
                replacement.attach_request_iter(requests)
                return replacement

            patched_grpc["stub"].StreamChannel.side_effect = open_stream
            replacement.responses.put(_make_arbitration_response(1, (1, 0)))
            client.connect(timeout=2.0)
            waiters = [pool.submit(client.expect_packet_in, timeout=2.0) for _ in range(2)]
            for _ in waiters:
                registrations.get(timeout=1.0)
            packet = p4runtime_pb2.StreamMessageResponse()
            packet.packet.payload = b"new connection"
            replacement.responses.put(packet)
            assert [waiter.result(timeout=1.0) for waiter in waiters] == [
                (b"new connection", {}),
                (b"new connection", {}),
            ]
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


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("has_config", [False, True])
def test_get_pipeline_config_rejects_a_response_without_p4info(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    cached: bool,
    has_config: bool,
) -> None:
    client, grpc_mocks, p4info_path, json_path = connected_client
    response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    if has_config:
        response.config.p4_device_config = b"{}"
    grpc_mocks["stub"].GetForwardingPipelineConfig.return_value = response
    try:
        if cached:
            client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path)
            assert "MyIngress.ipv4_lpm" in client.index.table_names

        message = (
            "returned no P4Info" if has_config else "returned no forwarding pipeline configuration"
        )
        with pytest.raises(P4RuntimeError, match=message):
            client.get_pipeline_config()
        with pytest.raises(P4RuntimeError, match="no pipeline is set"):
            _ = client.index
        assert client.is_connected()
    finally:
        client.disconnect()


def test_get_pipeline_config_accepts_p4info_without_tables_or_device_config(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client, grpc_mocks, _, _ = connected_client
    response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    response.config.p4info.pkg_info.name = "tableless"
    grpc_mocks["stub"].GetForwardingPipelineConfig.return_value = response
    try:
        config, index = client.get_pipeline_config()
        assert config == b""
        assert client.index is index
        assert index.raw.pkg_info.name == "tableless"
        assert index.table_names == []
    finally:
        client.disconnect()


@pytest.mark.parametrize("p4info_changed", [False, True])
def test_get_pipeline_config_keeps_inflight_reads_when_p4info_matches(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path], p4info_changed: bool
) -> None:
    client, mocks, p4info_path, json_path = connected_client
    p4info = _build_p4info_with_counter()
    p4info_path.write_text(str(p4info))
    client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path)
    previous = client.index
    if p4info_changed:
        p4info.tables[0].preamble.id = 4001
        p4info.counters[0].preamble.id = 5001
    readback = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    readback.config.p4info.CopyFrom(p4info)
    readback.config.p4_device_config = b'{"updated":"json"}'
    readback.config.cookie.cookie = 12
    stub = mocks["stub"]
    stub.GetForwardingPipelineConfig.return_value = readback
    table_reading, counter_reading, release = (threading.Event() for _ in range(3))
    table, counter = "MyIngress.ipv4_lpm", "MyIngress.ingress_pkts"

    def hold_read(request: Any, *, timeout: float) -> Iterator[Any]:
        response = p4runtime_pb2.ReadResponse()
        entity = response.entities.add()
        query = request.entities[0]
        if query.HasField("table_entry"):
            entity.table_entry.table_id = query.table_entry.table_id
            waiting = table_reading
        else:
            entity.counter_entry.counter_id = query.counter_entry.counter_id
            entity.counter_entry.index.index = 4
            entity.counter_entry.data.packet_count = 13
            waiting = counter_reading
        yield response
        waiting.set()
        assert release.wait(timeout=3.0)

    stub.Read.side_effect = hold_read
    with ThreadPoolExecutor(max_workers=2) as workers:
        try:
            entries = workers.submit(client.list_table_entries, table)
            count = workers.submit(client.read_counter, counter, 4)
            assert table_reading.wait(timeout=1.0)
            assert counter_reading.wait(timeout=1.0)
            device_config, index = client.get_pipeline_config()
            assert device_config == b'{"updated":"json"}'
            assert client.index is index and index.raw == p4info
            release.set()
            if p4info_changed:
                assert index is not previous
                for pending in (entries, count):
                    with pytest.raises(P4RuntimeError, match="P4Info changed"):
                        pending.result(timeout=1.0)
            else:
                assert [entry["table"] for entry in entries.result(timeout=1.0)] == [table]
                assert count.result(timeout=1.0).packet_count == 13
                assert index is previous
        finally:
            release.set()
            client.disconnect()


def _replacement_connection(mocks: dict[str, Any]) -> tuple[MagicMock, FakeStreamCall]:
    stream = FakeStreamCall()
    stream.responses.put(_make_arbitration_response(0, (1, 0)))

    def open_stream(requests: Iterator[Any]) -> FakeStreamCall:
        stream.attach_request_iter(requests)
        return stream

    stub = MagicMock(StreamChannel=MagicMock(side_effect=open_stream))
    mocks["channel_factory"].return_value = MagicMock(name="replacement-channel")
    mocks["stub_factory"].return_value = stub
    return stub, stream


@pytest.mark.parametrize("p4info", ["same", "changed", "missing", "unconfigured-bmv2"])
def test_reconnect_reads_current_p4info_before_reporting_connected(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path], p4info: str
) -> None:
    client = _push_pipeline(connected_client)
    _, mocks, _, _ = connected_client
    mocks["stub"].GetForwardingPipelineConfig.assert_not_called()
    previous = client.index
    client.disconnect()
    new_stub, _ = _replacement_connection(mocks)
    reply = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    if p4info in ("same", "changed"):
        reply.config.p4info.CopyFrom(previous.raw)
        if p4info == "changed":
            reply.config.p4info.tables[0].preamble.name = "NextIngress.ipv4_lpm"
    reading, release = threading.Event(), threading.Event()

    def read(_request: Any, *, timeout: float) -> Any:
        reading.set()
        assert release.wait(timeout=3.0)
        if p4info == "unconfigured-bmv2":
            raise _grpc_error(
                grpc.StatusCode.FAILED_PRECONDITION,
                "No forwarding pipeline config set for this device",
            )
        return reply

    new_stub.GetForwardingPipelineConfig.side_effect = read
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(client.connect, timeout=2.0)
        try:
            assert reading.wait(timeout=1.0)
            second = workers.submit(client.connect, timeout=0.0)
            assert not client.is_connected()
            assert not second.done()
            release.set()
            first.result(timeout=1.0)
            second.result(timeout=1.0)
            assert client.is_connected()
            client.connect()
            new_stub.GetForwardingPipelineConfig.assert_called_once()
            request = new_stub.GetForwardingPipelineConfig.call_args.args[0]
            assert request.device_id == 0
            assert request.response_type == request.P4INFO_AND_COOKIE
            assert 0 < new_stub.GetForwardingPipelineConfig.call_args.kwargs["timeout"] <= 2.0
            if p4info in ("same", "changed"):
                assert client.index.raw == reply.config.p4info
                assert (client.index is previous) == (p4info == "same")
            else:
                with pytest.raises(P4RuntimeError, match="no pipeline is set"):
                    _ = client.index
        finally:
            release.set()
            client.disconnect()


@pytest.mark.parametrize("ending", ["disconnect", "deadline", "rpc-error"])
def test_failed_reconnect_readback_does_not_restore_stale_p4info(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path], ending: str
) -> None:
    client = _push_pipeline(connected_client)
    _, mocks, _, _ = connected_client
    reply = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    reply.config.p4info.CopyFrom(client.index.raw)
    reply.config.p4info.tables[0].preamble.name = "NextIngress.ipv4_lpm"
    client.disconnect()
    new_stub, _ = _replacement_connection(mocks)
    reading, release = threading.Event(), threading.Event()
    error = (
        _grpc_error(grpc.StatusCode.DEADLINE_EXCEEDED, "gRPC deadline")
        if ending == "deadline"
        else _grpc_error(grpc.StatusCode.FAILED_PRECONDITION, "pipeline state unavailable")
    )

    def read(_request: Any, *, timeout: float) -> Any:
        reading.set()
        assert release.wait(timeout=3.0)
        if ending != "disconnect":
            raise error
        return reply

    new_stub.GetForwardingPipelineConfig.side_effect = read
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(client.connect, timeout=2.0)
        try:
            assert reading.wait(timeout=1.0)
            if ending == "disconnect":
                client.disconnect()
                kind, message = ConnectionError, "closed during P4Info readback"
            elif ending == "deadline":
                kind, message = ConnectionError, "P4Info readback timed out"
            else:
                kind, message = P4RuntimeError, "pipeline state unavailable"
            release.set()
            with pytest.raises(kind, match=message) as raised:
                pending.result(timeout=1.0)
            if ending != "disconnect":
                assert raised.value.__cause__ is error
            assert not client.is_connected()
            assert client._channel is None
            with pytest.raises(P4RuntimeError, match="no pipeline is set"):
                _ = client.index

            retry_stub, _ = _replacement_connection(mocks)
            retry_stub.GetForwardingPipelineConfig.return_value = reply
            client.connect()
            assert client.index.raw == reply.config.p4info
            retry_stub.GetForwardingPipelineConfig.assert_called_once()
        finally:
            release.set()
            client.disconnect()


def test_reconnect_readback_deadline_includes_waiting_for_pipeline_lock(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    _, mocks, _, _ = connected_client
    client.disconnect()
    new_stub, _ = _replacement_connection(mocks)
    try:
        with (
            client._pipeline_lock,
            pytest.raises(ConnectionError, match="P4Info readback timed out"),
        ):
            client.connect(timeout=0.1)
        assert not client.is_connected()
        new_stub.GetForwardingPipelineConfig.assert_not_called()
        with pytest.raises(P4RuntimeError, match="no pipeline is set"):
            _ = client.index
    finally:
        client.disconnect()


@pytest.mark.parametrize("installed", [False, True])
def test_verify_pipeline_keeps_the_current_index(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path], installed: bool
) -> None:
    client, grpc_mocks, p4info_path, json_path = connected_client
    rpc = grpc_mocks["stub"].SetForwardingPipelineConfig
    candidate = _build_p4info()
    candidate.tables[0].preamble.name = "Candidate.ipv4_lpm"
    candidate_path = p4info_path.with_name("candidate.p4info.txtpb")
    candidate_path.write_text(str(candidate))
    try:
        if installed:
            client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path)
        index = client.index if installed else None

        client.set_pipeline_config(bmv2_json=json_path, p4info=candidate_path, action="VERIFY")

        sent = rpc.call_args.args[0]
        assert sent.action == p4runtime_pb2.SetForwardingPipelineConfigRequest.VERIFY
        assert sent.config.p4info == candidate
        assert sent.config.p4_device_config == json_path.read_bytes()
        if installed:
            assert client.index is index
        else:
            with pytest.raises(P4RuntimeError, match="no pipeline is set"):
                _ = client.index
    finally:
        client.disconnect()


@pytest.mark.parametrize("index_state", ["saved", "unknown", "older"])
def test_commit_uses_the_switch_p4info(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path], index_state: str
) -> None:
    client, grpc_mocks, p4info_path, json_path = connected_client
    stub = grpc_mocks["stub"]
    candidate = _build_p4info()
    candidate.tables[0].preamble.name = "Candidate.ipv4_lpm"
    response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    response.config.p4info.CopyFrom(candidate)
    stub.GetForwardingPipelineConfig.return_value = response
    try:
        saved = None
        if index_state != "unknown":
            if index_state == "saved":
                p4info_path.write_text(str(candidate))
            client.set_pipeline_config(
                bmv2_json=json_path, p4info=p4info_path, action="VERIFY_AND_SAVE"
            )
            saved = client.index

        if index_state == "older":
            missing = p4info_path.with_name("unused")
            client.set_pipeline_config(
                bmv2_json=missing, p4info=missing, action="COMMIT", timeout=2.0
            )
        else:
            client.set_pipeline_config(action="COMMIT", timeout=2.0)

        sent = stub.SetForwardingPipelineConfig.call_args.args[0]
        assert sent.action == p4runtime_pb2.SetForwardingPipelineConfigRequest.COMMIT
        assert not sent.HasField("config")
        sent = stub.GetForwardingPipelineConfig.call_args.args[0]
        assert (
            sent.response_type == p4runtime_pb2.GetForwardingPipelineConfigRequest.P4INFO_AND_COOKIE
        )
        assert stub.GetForwardingPipelineConfig.call_args.kwargs == {"timeout": 2.0}
        assert client.index.raw == candidate
        if index_state == "saved":
            assert client.index is saved
    finally:
        client.disconnect()


@pytest.mark.parametrize("failure", ["commit", "refresh", "missing-p4info"])
def test_commit_failure_leaves_a_usable_or_empty_index(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path], failure: str
) -> None:
    client, grpc_mocks, p4info_path, json_path = connected_client
    stub = grpc_mocks["stub"]
    try:
        client.set_pipeline_config(
            bmv2_json=json_path, p4info=p4info_path, action="VERIFY_AND_SAVE"
        )
        saved = client.index
        if failure == "commit":
            stub.SetForwardingPipelineConfig.side_effect = _grpc_error(
                grpc.StatusCode.NOT_FOUND, "no saved pipeline"
            )
        elif failure == "refresh":
            stub.GetForwardingPipelineConfig.side_effect = _grpc_error(
                grpc.StatusCode.UNAVAILABLE, "could not get configuration"
            )
        else:
            stub.GetForwardingPipelineConfig.return_value = (
                p4runtime_pb2.GetForwardingPipelineConfigResponse()
            )
        message = "no saved pipeline" if failure == "commit" else "COMMIT succeeded"
        with pytest.raises(P4RuntimeError, match=message):
            client.set_pipeline_config(action="COMMIT")
        if failure == "commit":
            assert client.index is saved
            stub.GetForwardingPipelineConfig.assert_not_called()
        else:
            with pytest.raises(P4RuntimeError, match="no pipeline is set"):
                _ = client.index
    finally:
        client.disconnect()


@pytest.mark.parametrize("action", ["GET", "VERIFY_AND_COMMIT", "VERIFY", "COMMIT"])
@pytest.mark.parametrize("outcome", ["reply", "closed-channel"])
def test_pipeline_rpc_from_an_old_connection_does_not_install_a_stale_reply(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    action: str,
    outcome: str,
) -> None:
    client, grpc_mocks, p4info_path, json_path = connected_client
    client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path)
    current = client.index
    candidate = _build_p4info()
    candidate.tables[0].preamble.name = "Candidate.ipv4_lpm"
    p4info_path.write_text(str(candidate))

    started = threading.Event()
    reply = threading.Event()
    closed_channel = ValueError("Cannot invoke RPC on closed channel!")
    stale_response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    stale_response.config.p4info.CopyFrom(candidate)

    def hold_reply(_request: Any, *, timeout: float) -> Any:
        started.set()
        assert reply.wait(timeout=3.0)
        if outcome == "closed-channel":
            raise closed_channel
        if action == "GET":
            return stale_response
        return p4runtime_pb2.SetForwardingPipelineConfigResponse()

    old_stub = grpc_mocks["stub"]
    rpc = (
        old_stub.GetForwardingPipelineConfig
        if action == "GET"
        else old_stub.SetForwardingPipelineConfig
    )
    rpc.side_effect = hold_reply
    old_stub.SetForwardingPipelineConfig.reset_mock()
    current_response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    current_response.config.p4info.CopyFrom(current.raw)
    current_response.config.p4info.tables[0].preamble.name = "Current.ipv4_lpm"
    files = {} if action == "COMMIT" else {"bmv2_json": json_path, "p4info": p4info_path}
    with ThreadPoolExecutor(max_workers=2) as worker:
        try:
            if action == "GET":
                pending = worker.submit(client.get_pipeline_config)
            else:
                pending = worker.submit(client.set_pipeline_config, action=action, **files)
            assert started.wait(timeout=1.0)
            client.disconnect()
            new_stub, stream = _replacement_connection(grpc_mocks)
            new_stub.GetForwardingPipelineConfig.return_value = current_response
            reconnecting = worker.submit(client.connect, timeout=2.0)
            assert stream.read_started.wait(timeout=1.0)
            new_stub.GetForwardingPipelineConfig.assert_not_called()
            assert not reconnecting.done()
            reply.set()

            message = (
                "COMMIT succeeded. Connection closed during P4Info refresh."
                if action == "COMMIT" and outcome == "reply"
                else "closed during a pipeline operation"
            )
            with pytest.raises(ConnectionError, match=message) as raised:
                pending.result(timeout=1.0)
            if outcome == "closed-channel":
                assert raised.value.__context__ is closed_channel
            reconnecting.result(timeout=1.0)
            assert client.index.raw == current_response.config.p4info
            rpc.assert_called_once()
            if action == "GET":
                old_stub.SetForwardingPipelineConfig.assert_not_called()
            else:
                old_stub.GetForwardingPipelineConfig.assert_not_called()
            new_stub.GetForwardingPipelineConfig.assert_called_once()
            new_stub.SetForwardingPipelineConfig.assert_not_called()

            client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path)
            assert client.index.raw == candidate
            new_stub.SetForwardingPipelineConfig.assert_called_once()
        finally:
            reply.set()
            client.disconnect()


@pytest.mark.parametrize("operation", ["get", "push", "commit"])
@pytest.mark.parametrize("local_error", [ValueError, TypeError])
def test_pipeline_argument_error_on_active_connection_is_preserved(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    operation: str,
    local_error: type[ValueError | TypeError],
) -> None:
    client, grpc_mocks, p4info_path, json_path = connected_client
    stub = grpc_mocks["stub"]
    error = local_error("invalid RPC timeout")
    client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path)
    original_index = client.index
    try:
        with pytest.raises(local_error, match="invalid RPC timeout") as raised:
            if operation == "get":
                stub.GetForwardingPipelineConfig.side_effect = error
                client.get_pipeline_config()
            else:
                stub.SetForwardingPipelineConfig.side_effect = error
                if operation == "commit":
                    client.set_pipeline_config(action="COMMIT")
                else:
                    client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path)
        assert raised.value is error
        assert client.is_connected() and client.index is original_index
        if operation == "commit":
            stub.GetForwardingPipelineConfig.assert_not_called()
    finally:
        client.disconnect()


@pytest.mark.parametrize(
    ("code", "error_type", "clears_index"),
    [
        (grpc.StatusCode.INVALID_ARGUMENT, P4RuntimeError, False),
        (grpc.StatusCode.FAILED_PRECONDITION, PipelineError, False),
        (grpc.StatusCode.PERMISSION_DENIED, NotPrimaryError, False),
        (grpc.StatusCode.NOT_FOUND, EntryNotFoundError, False),
        (grpc.StatusCode.ALREADY_EXISTS, DuplicateEntryError, False),
        (grpc.StatusCode.UNAUTHENTICATED, P4RuntimeError, False),
        (grpc.StatusCode.UNIMPLEMENTED, P4RuntimeError, False),
        (grpc.StatusCode.DEADLINE_EXCEEDED, P4RuntimeError, True),
        (grpc.StatusCode.CANCELLED, P4RuntimeError, True),
        (grpc.StatusCode.UNAVAILABLE, ConnectionError, True),
        (grpc.StatusCode.INTERNAL, P4RuntimeError, True),
        (grpc.StatusCode.UNKNOWN, P4RuntimeError, True),
        (None, P4RuntimeError, True),
    ],
)
def test_set_pipeline_config_preserves_rpc_error_and_invalidates_uncertain_index(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    code: grpc.StatusCode | None,
    error_type: type[P4RuntimeError],
    clears_index: bool,
) -> None:
    client, grpc_mocks, p4info_path, json_path = connected_client
    stub: MagicMock = grpc_mocks["stub"]
    try:
        original = _push_pipeline(connected_client).index
        error = grpc.RpcError("bad pipeline") if code is None else _grpc_error(code, "bad pipeline")
        stub.SetForwardingPipelineConfig.side_effect = error
        with pytest.raises(error_type, match="bad pipeline") as raised:
            client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path, timeout=2.0)
        assert type(raised.value) is error_type
        assert raised.value.__cause__ is error
        if clears_index:
            with pytest.raises(P4RuntimeError, match="no pipeline is set"):
                _ = client.index
        else:
            assert client.index is original
        assert client.is_connected()
        stub.GetForwardingPipelineConfig.assert_not_called()
    finally:
        client.disconnect()


@pytest.mark.parametrize(
    "action", ["VERIFY", "VERIFY_AND_SAVE", "VERIFY_AND_COMMIT", "COMMIT", "RECONCILE_AND_COMMIT"]
)
def test_sync_pipeline_deadline_checks_queued_reads_and_can_recover_by_readback(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
    action: str,
) -> None:
    client, mocks, p4info_path, json_path = connected_client
    p4info_path.write_text(str(_build_p4info_with_counter()))
    client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path)
    previous = client.index
    stub = mocks["stub"]
    applied, release, prepared = (threading.Event() for _ in range(3))
    table, counter = "MyIngress.ipv4_lpm", "MyIngress.ingress_pkts"
    candidate = _build_p4info_with_counter()
    candidate.tables[0].preamble.id = 3001
    candidate.counters[0].preamble.id = 5001
    p4info_path.write_text(str(candidate))
    deadline = _grpc_error(grpc.StatusCode.DEADLINE_EXCEEDED, "pipeline deadline")

    def lose_reply(_request: Any, *, timeout: float) -> None:
        applied.set()
        assert release.wait(timeout=3.0)
        raise deadline

    capture_context = client._indexed_context

    def note_preparation() -> Any:
        context = capture_context()
        prepared.set()
        return context

    stub.SetForwardingPipelineConfig.side_effect = lose_reply
    stub.Read.return_value = iter(())
    mocker.patch.object(client, "_indexed_context", side_effect=note_preparation)
    files = {} if action == "COMMIT" else {"p4info": p4info_path, "bmv2_json": json_path}
    with ThreadPoolExecutor(max_workers=2) as workers:
        try:
            pipeline = workers.submit(client.set_pipeline_config, action=action, **files)
            assert applied.wait(timeout=1.0)
            read = workers.submit(client.list_table_entries, table)
            assert prepared.wait(timeout=1.0)
            stub.Read.assert_not_called()

            release.set()
            with pytest.raises(P4RuntimeError, match="pipeline deadline") as raised:
                pipeline.result(timeout=1.0)
            assert raised.value.__cause__ is deadline
            stub.GetForwardingPipelineConfig.assert_not_called()
            if action == "VERIFY":
                assert client.index is previous
                assert read.result(timeout=1.0) == []
                stub.Read.assert_called_once()
            else:
                with pytest.raises(P4RuntimeError, match="P4Info changed"):
                    read.result(timeout=1.0)
                with pytest.raises(P4RuntimeError, match="no pipeline is set"):
                    client.read_counter(counter)
                stub.Read.assert_not_called()

                response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
                response.config.p4info.CopyFrom(candidate)
                stub.GetForwardingPipelineConfig.return_value = response
                _, current = client.get_pipeline_config()
                assert client.index is current and current.raw == candidate
                assert client.read_counter(counter, 3).packet_count == 0
                assert stub.Read.call_args.args[0].entities[0].counter_entry.counter_id == 5001
        finally:
            release.set()
            client.disconnect()


def test_interrupted_pipeline_rpc_discards_index(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client, mocks, p4info_path, json_path = connected_client
    _push_pipeline(connected_client)
    mocks["stub"].SetForwardingPipelineConfig.side_effect = KeyboardInterrupt()
    try:
        with pytest.raises(KeyboardInterrupt):
            client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path)
        with pytest.raises(P4RuntimeError, match="no pipeline is set"):
            _ = client.index
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
        if operation == "pipeline" and wrapped:
            with pytest.raises(P4RuntimeError, match="no pipeline is set"):
                _ = client.index
        else:
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


@pytest.mark.parametrize("change", ["reconnect", "p4info"])
@pytest.mark.parametrize("operation", ["insert", "modify", "delete", "clear"])
def test_table_write_keeps_the_connection_and_p4info_used_to_encode_it(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
    operation: str,
    change: str,
) -> None:
    client = _push_pipeline(connected_client)
    _, grpc_mocks, p4info_path, json_path = connected_client
    old_stub = grpc_mocks["stub"]
    current_stub = old_stub
    table = "MyIngress.ipv4_lpm"
    key = {"hdr.ipv4.dstAddr": "10.0.1.0/24"}
    action = "MyIngress.set_egress_port"
    response = p4runtime_pb2.ReadResponse()
    entry = response.entities.add().table_entry
    entry.table_id = 2001
    field = entry.match.add()
    field.field_id = 1
    field.lpm.value = b"\x0a\x00\x01\x00"
    field.lpm.prefix_len = 24
    old_stub.Read.return_value = iter([response])

    encoding = threading.Event()
    release = threading.Event()
    index = client.index
    encode = index.encode_match

    def hold_encoding(table_name: str, match: Mapping[str, object]) -> list[Any]:
        encoding.set()
        assert release.wait(timeout=3.0)
        return encode(table_name, match)

    mocker.patch.object(index, "encode_match", side_effect=hold_encoding)

    def write() -> None:
        if operation == "clear":
            client.clear_table(table)
        elif operation == "delete":
            client.delete_table_entry(table, key)
        else:
            getattr(client, f"{operation}_table_entry")(table, key, action, {"port": 2})

    with ThreadPoolExecutor(max_workers=1) as worker:
        try:
            pending = worker.submit(write)
            assert encoding.wait(timeout=1.0)
            if change == "reconnect":
                client.disconnect()
                current_stub, _ = _replacement_connection(grpc_mocks)
                reply = p4runtime_pb2.GetForwardingPipelineConfigResponse()
                reply.config.p4info.CopyFrom(_build_p4info())
                current_stub.GetForwardingPipelineConfig.return_value = reply
                client.connect(timeout=1.0)
            else:
                new_info = _build_p4info()
                new_info.tables[0].preamble.id = 4001
                p4info_path.write_text(str(new_info))
                client.set_pipeline_config(p4info=p4info_path, bmv2_json=json_path)
            release.set()

            error = ConnectionError if change == "reconnect" else P4RuntimeError
            message = "closed during a table write" if change == "reconnect" else "P4Info changed"
            with pytest.raises(error, match=message):
                pending.result(timeout=1.0)
            old_stub.Write.assert_not_called()
            current_stub.Write.assert_not_called()

            client.insert_table_entry(table, key, action, {"port": 3})
            current_stub.Write.assert_called_once()
            written = current_stub.Write.call_args.args[0].updates[0].entity.table_entry
            assert written.table_id == (2001 if change == "reconnect" else 4001)
        finally:
            release.set()
            client.disconnect()


@pytest.mark.parametrize(
    ("operation", "change"),
    [
        ("list-one", "reconnect"),
        ("list-one", "p4info"),
        ("list-all", "reconnect"),
        ("list-all", "p4info"),
        ("clear-empty", "reconnect"),
        ("clear-empty", "p4info"),
        ("before-read", "p4info"),
        ("invalid-reply", "p4info"),
    ],
)
def test_table_reads_keep_their_connection_and_p4info(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
    operation: str,
    change: str,
) -> None:
    client = _push_pipeline(connected_client)
    _, mocks, _, _ = connected_client
    original_stub = current_stub = mocks["stub"]
    table = "MyIngress.ipv4_lpm"
    original_id, updated_id = 2001, 4001
    active_id = original_id
    started, release = threading.Event(), threading.Event()

    def response(table_id: int) -> Any:
        reply = p4runtime_pb2.ReadResponse()
        entry = reply.entities.add().table_entry
        entry.table_id = table_id
        field = entry.match.add()
        field.field_id = 1
        field.lpm.value = b"\x0a\x00\x01\x00"
        field.lpm.prefix_len = 24
        return reply

    def wait_for_change() -> None:
        started.set()
        assert release.wait(timeout=3.0)

    def hold_response(_request: Any, *, timeout: float) -> Iterator[Any]:
        if operation == "invalid-reply":
            wait_for_change()
            yield response(updated_id)
            return
        if operation != "clear-empty":
            yield response(original_id)
        if operation != "before-read":
            wait_for_change()

    original_stub.Read.side_effect = hold_response
    if operation == "before-read":
        index = client.index
        lookup = index.table_id

        def hold_lookup(name: str) -> int:
            wait_for_change()
            return lookup(name)

        mocker.patch.object(index, "table_id", side_effect=hold_lookup)

    with ThreadPoolExecutor(max_workers=1) as worker:
        try:
            if operation == "clear-empty":
                pending = worker.submit(client.clear_table, table)
            else:
                requested_table = None if operation == "list-all" else table
                pending = worker.submit(client.list_table_entries, requested_table)
            assert started.wait(timeout=1.0)
            if change == "p4info":
                active_id = updated_id
            p4info = _build_p4info()
            p4info.tables[0].preamble.id = active_id
            reply = p4runtime_pb2.GetForwardingPipelineConfigResponse()
            reply.config.p4info.CopyFrom(p4info)
            if change == "reconnect":
                client.disconnect()
                current_stub, _ = _replacement_connection(mocks)
                current_stub.GetForwardingPipelineConfig.return_value = reply
                client.connect(timeout=1.0)
            else:
                current_stub.GetForwardingPipelineConfig.return_value = reply
                client.get_pipeline_config()
            release.set()

            error = ConnectionError if change == "reconnect" else P4RuntimeError
            message = "closed during a table read" if change == "reconnect" else "P4Info changed"
            with pytest.raises(error, match=message):
                pending.result(timeout=1.0)
            assert original_stub.Read.call_count == int(operation != "before-read")
            original_stub.Write.assert_not_called()
            if change == "reconnect":
                current_stub.Read.assert_not_called()
                current_stub.Write.assert_not_called()

            current_stub.Read.side_effect = None
            current_stub.Read.return_value = iter([response(active_id)])
            if operation == "clear-empty":
                assert client.clear_table(table) == 1
                current_stub.Write.assert_called_once()
                update = current_stub.Write.call_args.args[0].updates[0]
                assert update.entity.table_entry.table_id == active_id
            else:
                entries = client.list_table_entries(None if operation == "list-all" else table)
                assert [entry["table"] for entry in entries] == [table]
                current_stub.Write.assert_not_called()
            request = current_stub.Read.call_args.args[0]
            assert request.entities[0].table_entry.table_id == (
                0 if operation == "list-all" else active_id
            )
        finally:
            release.set()
            client.disconnect()


@pytest.mark.parametrize(
    ("operation", "action"),
    [
        ("table", "VERIFY"),
        ("table", "VERIFY_AND_COMMIT"),
        ("clear", "COMMIT"),
        ("counter", "VERIFY_AND_COMMIT"),
        ("counter-all", "COMMIT"),
    ],
)
def test_sync_indexed_write_waits_for_pipeline_and_rechecks_p4info(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
    operation: str,
    action: str,
) -> None:
    client, mocks, p4info_path, json_path = connected_client
    initial = _build_p4info_with_counter()
    p4info_path.write_text(str(initial))
    client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path)
    original = client.index
    candidate = _build_p4info_with_counter()
    candidate.tables[0].preamble.id = 4001
    candidate.counters[0].preamble.id = 5001
    p4info_path.write_text(str(candidate))
    stub = mocks["stub"]
    pushing, release_push = threading.Event(), threading.Event()
    refreshing, release_refresh = threading.Event(), threading.Event()
    queued, sent = threading.Event(), threading.Event()
    allow_dispatch = threading.Event()
    table, counter = "MyIngress.ipv4_lpm", "MyIngress.ingress_pkts"
    key = {"hdr.ipv4.dstAddr": "10.0.0.1/32"}

    def hold_push(_request: Any, *, timeout: float) -> None:
        pushing.set()
        assert release_push.wait(timeout=3.0)

    def hold_refresh(_request: Any, *, timeout: float) -> Any:
        refreshing.set()
        assert release_refresh.wait(timeout=3.0)
        response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
        response.config.p4info.CopyFrom(candidate)
        return response

    def read_entries(request: Any, *, timeout: float) -> Iterator[Any]:
        response = p4runtime_pb2.ReadResponse()
        requested = request.entities[0]
        entity = response.entities.add()
        if requested.HasField("counter_entry"):
            entity.counter_entry.counter_id = requested.counter_entry.counter_id
            entity.counter_entry.index.index = 3
        else:
            entry = entity.table_entry
            entry.table_id = requested.table_entry.table_id
            field = entry.match.add()
            field.field_id = 1
            field.lpm.value = b"\x0a\x00\x00\x01"
            field.lpm.prefix_len = 32
        return iter([response])

    dispatch = client._write_indexed_request

    def note_dispatch(*args: Any, **kwargs: Any) -> None:
        queued.set()
        assert allow_dispatch.wait(timeout=3.0)
        dispatch(*args, **kwargs)

    def send(*args: Any, **kwargs: Any) -> None:
        sent.set()

    def write() -> None:
        if operation == "table":
            client.delete_table_entry(table, key)
        elif operation == "clear":
            assert client.clear_table(table) == 1
        else:
            client.reset_counter(counter, 3 if operation == "counter" else None)

    stub.SetForwardingPipelineConfig.side_effect = hold_push
    stub.GetForwardingPipelineConfig.side_effect = hold_refresh
    stub.Read.side_effect = read_entries
    stub.Write.side_effect = send
    mocker.patch.object(client, "_write_indexed_request", side_effect=note_dispatch)
    files = {} if action == "COMMIT" else {"bmv2_json": json_path, "p4info": p4info_path}
    with ThreadPoolExecutor(max_workers=2) as workers:
        try:
            if operation in ("clear", "counter-all"):
                # Finish the read so this test can pause the following write.
                pending = workers.submit(write)
                assert queued.wait(timeout=1.0)
            pipeline = workers.submit(client.set_pipeline_config, action=action, **files)
            assert pushing.wait(timeout=1.0)
            if operation not in ("clear", "counter-all"):
                pending = workers.submit(write)
            assert queued.wait(timeout=1.0)
            allow_dispatch.set()
            assert not sent.wait(timeout=0.03)
            stub.Write.assert_not_called()

            release_push.set()
            if action == "COMMIT":
                assert refreshing.wait(timeout=1.0)
                stub.Write.assert_not_called()
                release_refresh.set()
            pipeline.result(timeout=1.0)

            if action == "VERIFY":
                pending.result(timeout=1.0)
                assert client.index is original
                expected_table_id = 2001
            else:
                message = (
                    "P4Info changed during counter reset"
                    if operation.startswith("counter")
                    else "P4Info changed during table write"
                )
                with pytest.raises(P4RuntimeError, match=message):
                    pending.result(timeout=1.0)
                stub.Write.assert_not_called()
                write()
                expected_table_id = 4001

            stub.Write.assert_called_once()
            entity = stub.Write.call_args.args[0].updates[0].entity
            if operation.startswith("counter"):
                assert entity.counter_entry.counter_id == 5001
            else:
                assert entity.table_entry.table_id == expected_table_id
        finally:
            allow_dispatch.set()
            release_push.set()
            release_refresh.set()
            client.disconnect()


@pytest.mark.parametrize(
    ("operation", "action", "empty_result"),
    [
        ("table-one", "VERIFY", []),
        ("table-all", "VERIFY_AND_COMMIT", []),
        ("counter-cell", "VERIFY_AND_COMMIT", 0),
        ("counter-all", "COMMIT", {}),
        ("clear-empty", "COMMIT", 0),
        ("reset-empty", "COMMIT", None),
    ],
)
def test_sync_indexed_read_waits_for_pipeline_and_rechecks_p4info(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
    operation: str,
    action: str,
    empty_result: object,
) -> None:
    client, mocks, p4info_path, json_path = connected_client
    p4info_path.write_text(str(_build_p4info_with_counter()))
    client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path)
    original = client.index
    candidate = _build_p4info_with_counter()
    candidate.tables[0].preamble.id = 4001
    candidate.counters[0].preamble.id = 5001
    p4info_path.write_text(str(candidate))
    stub = mocks["stub"]
    pushing, release_push = threading.Event(), threading.Event()
    refreshing, release_refresh = threading.Event(), threading.Event()
    prepared, sent = threading.Event(), threading.Event()
    table, counter = "MyIngress.ipv4_lpm", "MyIngress.ingress_pkts"
    capture_context = client._indexed_context

    def hold_push(_request: Any, *, timeout: float) -> None:
        pushing.set()
        assert release_push.wait(timeout=3.0)

    def hold_refresh(_request: Any, *, timeout: float) -> Any:
        refreshing.set()
        assert release_refresh.wait(timeout=3.0)
        response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
        response.config.p4info.CopyFrom(candidate)
        return response

    def prepare() -> Any:
        result = capture_context()
        prepared.set()
        return result

    def empty_read(_request: Any, *, timeout: float) -> Iterator[Any]:
        sent.set()
        return iter(())

    def read() -> Any:
        if operation == "clear-empty":
            return client.clear_table(table)
        if operation == "reset-empty":
            return client.reset_counter(counter)
        if operation == "counter-cell":
            return client.read_counter(counter, 3).packet_count
        if operation == "counter-all":
            return client.read_counter(counter)
        return client.list_table_entries(table if operation == "table-one" else None)

    stub.SetForwardingPipelineConfig.side_effect = hold_push
    stub.GetForwardingPipelineConfig.side_effect = hold_refresh
    stub.Read.side_effect = empty_read
    mocker.patch.object(client, "_indexed_context", side_effect=prepare)
    files = {} if action == "COMMIT" else {"bmv2_json": json_path, "p4info": p4info_path}
    with ThreadPoolExecutor(max_workers=2) as workers:
        try:
            pipeline = workers.submit(client.set_pipeline_config, action=action, **files)
            assert pushing.wait(timeout=1.0)
            pending = workers.submit(read)
            assert prepared.wait(timeout=1.0)
            assert not sent.wait(timeout=0.03)
            stub.Read.assert_not_called()

            release_push.set()
            if action == "COMMIT":
                assert refreshing.wait(timeout=1.0)
                stub.Read.assert_not_called()
                release_refresh.set()
            pipeline.result(timeout=1.0)
            if action == "VERIFY":
                assert pending.result(timeout=1.0) == empty_result
                assert client.index is original
            else:
                with pytest.raises(P4RuntimeError, match=r"P4Info changed during .* read"):
                    pending.result(timeout=1.0)
                stub.Read.assert_not_called()
                assert read() == empty_result
            stub.Read.assert_called_once()
            stub.Write.assert_not_called()
        finally:
            release_push.set()
            release_refresh.set()
            client.disconnect()


@pytest.mark.parametrize("ending", ["success", "rpc-error"])
@pytest.mark.parametrize("operation", ["read", "write"])
def test_sync_pipeline_waits_for_all_inflight_indexed_operations(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    ending: str,
    operation: str,
) -> None:
    client = _push_pipeline(connected_client)
    _, mocks, p4info_path, json_path = connected_client
    stub = mocks["stub"]
    stub.SetForwardingPipelineConfig.reset_mock()
    other = P4RuntimeClient("127.0.0.1:50052", device_id=0)
    other._connected = True
    other._index = client.index
    other._stub = MagicMock()

    entered = (threading.Event(), threading.Event())
    release = (threading.Event(), threading.Event())
    push_started = threading.Event()
    table = "MyIngress.ipv4_lpm"

    def hold(cell: int) -> None:
        entered[cell].set()
        assert release[cell].wait(timeout=3.0)
        if ending == "rpc-error" and cell == 0:
            raise _grpc_error(grpc.StatusCode.DEADLINE_EXCEEDED, "RPC deadline")

    def hold_write(request: Any, *, timeout: float) -> None:
        cell = request.updates[0].entity.table_entry.match[0].lpm.value[-1] - 1
        hold(cell)

    def hold_read(request: Any, *, timeout: float) -> Iterator[Any]:
        yield p4runtime_pb2.ReadResponse()
        hold(int(request.entities[0].table_entry.table_id != 2001))

    def push(_request: Any, *, timeout: float) -> None:
        push_started.set()

    def run(cell: int) -> None:
        if operation == "read":
            client.list_table_entries(table if cell == 1 else "MyIngress.ternary_acl")
        else:
            client.delete_table_entry(table, {"hdr.ipv4.dstAddr": f"10.0.0.{cell}/32"})

    stub.Write.side_effect = hold_write
    stub.Read.side_effect = hold_read
    stub.SetForwardingPipelineConfig.side_effect = push
    with ThreadPoolExecutor(max_workers=4) as workers:
        try:
            calls = [workers.submit(run, cell) for cell in (1, 2)]
            assert all(event.wait(timeout=1.0) for event in entered)
            pipeline = workers.submit(
                client.set_pipeline_config, bmv2_json=json_path, p4info=p4info_path
            )
            unrelated = workers.submit(
                other.delete_table_entry, table, {"hdr.ipv4.dstAddr": "10.0.0.3/32"}
            )
            unrelated.result(timeout=1.0)
            other._stub.Write.assert_called_once()
            assert not push_started.wait(timeout=0.03)

            release[0].set()
            if ending == "rpc-error":
                with pytest.raises(P4RuntimeError, match="RPC deadline"):
                    calls[0].result(timeout=1.0)
            else:
                calls[0].result(timeout=1.0)
            assert not push_started.is_set()

            release[1].set()
            calls[1].result(timeout=1.0)
            pipeline.result(timeout=1.0)
            stub.SetForwardingPipelineConfig.assert_called_once()
        finally:
            for event in release:
                event.set()
            client.disconnect()
            other.disconnect()


@pytest.mark.parametrize("operation", ["insert", "modify", "delete"])
@pytest.mark.parametrize("wildcard", [False, True])
@pytest.mark.parametrize("priority", [1, 17, (1 << 31) - 1])
def test_optional_table_write_preserves_priority(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    operation: str,
    wildcard: bool,
    priority: int,
) -> None:
    client = _push_pipeline(connected_client)
    stub: MagicMock = connected_client[1]["stub"]
    match = {} if wildcard else {"hdr.ethernet.etherType": 0x0800}
    method = getattr(client, f"{operation}_table_entry")
    try:
        if operation == "delete":
            method("MyIngress.optional_acl", match, priority=priority)
        else:
            method("MyIngress.optional_acl", match, "NoAction", priority=priority)
        stub.Write.assert_called_once()
        request = stub.Write.call_args.args[0]
        update = request.updates[0]
        assert update.type == p4runtime_pb2.Update.Type.Value(operation.upper())
        entry = update.entity.table_entry
        assert entry.table_id == 2003
        assert entry.priority == priority
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


@pytest.mark.parametrize("change", ["reconnect", "p4info"])
@pytest.mark.parametrize(
    ("operation", "pause_at_read"),
    [
        ("read", False),
        ("read", True),
        ("reset-cell", False),
        ("reset-all", False),
        ("reset-all", True),
    ],
)
def test_counter_operations_keep_their_connection_and_p4info(
    connected_client_with_counter: tuple[P4RuntimeClient, dict[str, Any]],
    mocker: MockerFixture,
    change: str,
    operation: str,
    pause_at_read: bool,
) -> None:
    client, mocks = connected_client_with_counter
    initial_stub = current_stub = mocks["stub"]
    counter = "MyIngress.ingress_pkts"
    original_id = 3001
    active_id = original_id
    started, release = threading.Event(), threading.Event()

    def response(counter_id: int) -> Iterator[Any]:
        reply = p4runtime_pb2.ReadResponse()
        for cell in (7, 4):
            entry = reply.entities.add().counter_entry
            entry.counter_id = counter_id
            entry.index.index = cell
            entry.data.packet_count = 13
        return iter([reply])

    def read_response(_request: Any, *, timeout: float) -> Iterator[Any]:
        if pause_at_read:
            started.set()
            assert release.wait(timeout=3.0)
        return response(original_id)

    initial_stub.Read.side_effect = read_response
    if not pause_at_read:
        index = client.index
        lookup = index.counter_id

        def hold_lookup(name: str) -> int:
            started.set()
            assert release.wait(timeout=3.0)
            return lookup(name)

        mocker.patch.object(index, "counter_id", side_effect=hold_lookup)

    def run() -> None:
        if operation == "read":
            client.read_counter(counter, 4)
        else:
            client.reset_counter(counter, 4 if operation == "reset-cell" else None)

    with ThreadPoolExecutor(max_workers=1) as worker:
        try:
            pending = worker.submit(run)
            assert started.wait(timeout=1.0)
            if change == "reconnect":
                client.disconnect()
                current_stub, _ = _replacement_connection(mocks)
                reply = p4runtime_pb2.GetForwardingPipelineConfigResponse()
                reply.config.p4info.CopyFrom(_build_p4info_with_counter())
                current_stub.GetForwardingPipelineConfig.return_value = reply
                current_stub.Read.return_value = response(original_id)
                client.connect(timeout=1.0)
            else:
                active_id = 4001
                p4info = _build_p4info_with_counter()
                p4info.counters[0].preamble.id = active_id
                reply = p4runtime_pb2.GetForwardingPipelineConfigResponse()
                reply.config.p4info.CopyFrom(p4info)
                current_stub.GetForwardingPipelineConfig.return_value = reply
                client.get_pipeline_config()
            release.set()

            error = ConnectionError if change == "reconnect" else P4RuntimeError
            message = "closed during a counter" if change == "reconnect" else "P4Info changed"
            with pytest.raises(error, match=message):
                pending.result(timeout=1.0)
            assert initial_stub.Read.call_count == int(pause_at_read)
            initial_stub.Write.assert_not_called()
            if change == "reconnect":
                current_stub.Read.assert_not_called()
                current_stub.Write.assert_not_called()

            current_stub.Read.side_effect = None
            current_stub.Read.return_value = response(active_id)
            client.reset_counter(counter)
            read = current_stub.Read.call_args.args[0]
            assert read.entities[0].counter_entry.counter_id == active_id
            current_stub.Write.assert_called_once()
            updates = current_stub.Write.call_args.args[0].updates
            assert [
                (item.entity.counter_entry.counter_id, item.entity.counter_entry.index.index)
                for item in updates
            ] == [(active_id, 4), (active_id, 7)]
        finally:
            release.set()
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


@pytest.mark.parametrize(
    ("method", "args", "rpc", "pause"),
    [
        ("list_multicast_groups", (), "Read", "request"),
        ("add_multicast_group", (7, [1, 2]), "Write", "request"),
        ("modify_multicast_group", (7, [3]), "Write", "request"),
        ("delete_multicast_group", (7,), "Write", "request"),
        ("list_multicast_groups", (), "Read", "reply"),
        ("list_multicast_groups", (), "Read", "closed-channel"),
        ("add_multicast_group", (7, [1, 2]), "Write", "closed-channel"),
    ],
)
def test_multicast_operations_keep_their_connection(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
    method: str,
    args: tuple[object, ...],
    rpc: str,
    pause: str,
) -> None:
    client, mocks, _, _ = connected_client
    old_stub = mocks["stub"]
    started, release = threading.Event(), threading.Event()

    reply = p4runtime_pb2.ReadResponse()
    group = reply.entities.add().packet_replication_engine_entry.multicast_group_entry
    group.multicast_group_id = 7
    group.replicas.add(egress_port=3, instance=1)

    def wait_for_reconnect() -> None:
        started.set()
        assert release.wait(timeout=3.0)

    if pause == "request":
        name = f"{rpc}Request"
        make_request = getattr(p4runtime_pb2, name)

        def hold_request() -> Any:
            wait_for_reconnect()
            return make_request()

        mocker.patch.object(p4runtime_pb2, name, side_effect=hold_request)
    else:

        def hold_rpc(_request: Any, *, timeout: float) -> Iterator[Any]:
            wait_for_reconnect()
            if pause == "closed-channel":
                raise ValueError("Cannot invoke RPC on closed channel!")
            return iter([reply])

        getattr(old_stub, rpc).side_effect = hold_rpc

    with ThreadPoolExecutor(max_workers=1) as worker:
        try:
            operation = getattr(client, method)
            pending = worker.submit(operation, *args)
            assert started.wait(timeout=1.0)
            client.disconnect()
            stream = FakeStreamCall()
            stream.responses.put(_make_arbitration_response(0, (1, 0)))

            def open_stream(requests: Iterator[Any]) -> FakeStreamCall:
                stream.attach_request_iter(requests)
                return stream

            replacement = MagicMock(StreamChannel=MagicMock(side_effect=open_stream))
            replacement.Read.return_value = iter([reply])
            mocks["channel_factory"].return_value = MagicMock(name="replacement-channel")
            mocks["stub_factory"].return_value = replacement
            client.connect(timeout=1.0)
            release.set()

            with pytest.raises(ConnectionError, match="closed during a multicast"):
                pending.result(timeout=1.0)
            assert getattr(old_stub, rpc).call_count == int(pause != "request")
            replacement.Read.assert_not_called()
            replacement.Write.assert_not_called()

            result = operation(*args)
            getattr(replacement, rpc).assert_called_once()
            if rpc == "Read":
                assert result == {7: [3]}
        finally:
            release.set()
            client.disconnect()


@pytest.mark.parametrize(
    ("match_type", "value"),
    [
        (p4info_pb2.MatchField.OPTIONAL, "10.0.0.1"),
        (p4info_pb2.MatchField.TERNARY, ("10.0.0.0", "255.255.0.0")),
        (p4info_pb2.MatchField.RANGE, (1024, 2048)),
    ],
)
@pytest.mark.parametrize("wildcard", [False, True])
@pytest.mark.parametrize("priority", [None, 17], ids=["missing", "provided"])
def test_delete_priority_table_validates_before_rpc(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    match_type: int,
    value: object,
    wildcard: bool,
    priority: int | None,
) -> None:
    client = _push_pipeline(connected_client)
    table = next(t for t in client.index.raw.tables if t.preamble.name == "MyIngress.ipv4_lpm")
    table.match_fields[0].match_type = match_type
    match = {} if wildcard else {"hdr.ipv4.dstAddr": value}
    try:
        if priority is None:
            with pytest.raises(EncodingError, match="priority"):
                client.delete_table_entry("MyIngress.ipv4_lpm", match)
            connected_client[1]["stub"].Write.assert_not_called()
        else:
            client.delete_table_entry("MyIngress.ipv4_lpm", match, priority=priority)
            connected_client[1]["stub"].Write.assert_called_once()
            update = connected_client[1]["stub"].Write.call_args.args[0].updates[0]
            assert update.type == p4runtime_pb2.Update.Type.DELETE
            entry = update.entity.table_entry
            assert entry.table_id == 2001
            assert entry.priority == priority
            assert len(entry.match) == (0 if wildcard else 1)
            assert not entry.HasField("action")
    finally:
        client.disconnect()


def test_delete_exact_table_does_not_require_priority(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    table = next(t for t in client.index.raw.tables if t.preamble.name == "MyIngress.ipv4_lpm")
    table.match_fields[0].match_type = p4info_pb2.MatchField.EXACT
    try:
        client.delete_table_entry("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.0.1"})
        connected_client[1]["stub"].Write.assert_called_once()
        entry = connected_client[1]["stub"].Write.call_args.args[0].updates[0].entity.table_entry
        assert entry.priority == 0
        assert entry.match[0].HasField("exact")
    finally:
        client.disconnect()


@pytest.mark.parametrize("operation", ["insert", "modify", "delete"])
@pytest.mark.parametrize(
    "priority",
    [
        0,
        -1,
        -(1 << 31),
        1 << 31,
        -(1 << 31) - 1,
        True,
        False,
        1.0,
        1.9,
        "10",
        b"10",
        float("nan"),
        float("inf"),
        -float("inf"),
    ],
)
def test_invalid_priority_does_not_write(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    operation: str,
    priority: Any,
) -> None:
    client = _push_pipeline(connected_client)
    method = getattr(client, f"{operation}_table_entry")
    match = {"hdr.ethernet.etherType": 0x0800}
    try:
        with pytest.raises(EncodingError, match="priority"):
            if operation == "delete":
                method("MyIngress.optional_acl", match, priority=priority)
            else:
                method("MyIngress.optional_acl", match, "NoAction", priority=priority)
        connected_client[1]["stub"].Write.assert_not_called()
    finally:
        client.disconnect()


@pytest.mark.parametrize("operation", ["insert", "modify", "delete"])
@pytest.mark.parametrize("match_type", [p4info_pb2.MatchField.EXACT, p4info_pb2.MatchField.LPM])
@pytest.mark.parametrize("priority", [0, False])
def test_exact_lpm_priority_must_be_none(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    operation: str,
    match_type: int,
    priority: int,
) -> None:
    client = _push_pipeline(connected_client)
    table = next(t for t in client.index.raw.tables if t.preamble.name == "MyIngress.ipv4_lpm")
    table.match_fields[0].match_type = match_type
    value = "10.0.0.1" if match_type == p4info_pb2.MatchField.EXACT else "10.0.0.0/24"
    match = {"hdr.ipv4.dstAddr": value}
    method = getattr(client, f"{operation}_table_entry")
    try:
        with pytest.raises(EncodingError, match="priority must be None"):
            if operation == "delete":
                method("MyIngress.ipv4_lpm", match, priority=priority)
            else:
                method("MyIngress.ipv4_lpm", match, "NoAction", priority=priority)
        connected_client[1]["stub"].Write.assert_not_called()
    finally:
        client.disconnect()
