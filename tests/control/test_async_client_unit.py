"""Unit tests for ``p4net.control.AsyncP4RuntimeClient``. All gRPC mocked."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest
from google.rpc import code_pb2, status_pb2
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2
from pytest_mock import MockerFixture

import p4net.control  # noqa: F401  (sets protobuf python-impl env var)
from p4net.control import (
    AsyncOperationCancelledError,
    AsyncP4RuntimeClient,
    ConnectionError,
    DuplicateEntryError,
    EncodingError,
    EntryNotFoundError,
    NoSuchRegisterError,
    NotPrimaryError,
    P4InfoIndex,
    P4RuntimeError,
    PipelineError,
)

# ---------------------------------------------------------------------------
# Helpers: build a minimal P4Info index with a table, an action, and a register.
# ---------------------------------------------------------------------------


def _make_p4info_index() -> P4InfoIndex:
    p = p4info_pb2.P4Info()
    a = p.actions.add()
    a.preamble.id = 1001
    a.preamble.name = "MyIngress.set_egress_port"
    param = a.params.add()
    param.id = 1
    param.name = "port"
    param.bitwidth = 9
    t = p.tables.add()
    t.preamble.id = 2001
    t.preamble.name = "MyIngress.ipv4_lpm"
    mf = t.match_fields.add()
    mf.id = 1
    mf.name = "hdr.ipv4.dstAddr"
    mf.bitwidth = 32
    mf.match_type = p4info_pb2.MatchField.LPM
    t.action_refs.add().id = 1001
    t_opt = p.tables.add()
    t_opt.preamble.id = 2002
    t_opt.preamble.name = "MyIngress.optional_acl"
    mf_opt = t_opt.match_fields.add()
    mf_opt.id = 1
    mf_opt.name = "hdr.ethernet.etherType"
    mf_opt.bitwidth = 16
    mf_opt.match_type = p4info_pb2.MatchField.OPTIONAL
    t_opt.action_refs.add().id = 1001
    r = p.registers.add()
    r.preamble.id = 4001
    r.preamble.name = "MyIngress.test_register"
    r.type_spec.bitstring.bit.bitwidth = 32
    r.size = 16
    return P4InfoIndex(p)


class _FakeStream:
    """Mimics the bidirectional StreamChannel object returned by stub.StreamChannel."""

    def __init__(self) -> None:
        self._responses: asyncio.Queue[Any] = asyncio.Queue()
        self._cancelled = False
        self.read_started = asyncio.Event()

    async def push_arbitration(self, code: int = 0) -> None:
        resp = p4runtime_pb2.StreamMessageResponse()
        resp.arbitration.device_id = 0
        resp.arbitration.election_id.high = 1
        resp.arbitration.election_id.low = 0
        resp.arbitration.status.code = code
        await self._responses.put(resp)

    async def push_packet(self, payload: bytes) -> None:
        resp = p4runtime_pb2.StreamMessageResponse()
        resp.packet.payload = payload
        await self._responses.put(resp)

    async def finish(self, error: BaseException | None = None) -> None:
        await self._responses.put(error if error is not None else StopAsyncIteration)

    def cancel(self) -> None:
        self._cancelled = True
        self._responses.put_nowait(StopAsyncIteration)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        self.read_started.set()
        item = await self._responses.get()
        if item is StopAsyncIteration or self._cancelled:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def patched_aio(mocker: MockerFixture) -> dict[str, Any]:
    """Patch grpc.aio.insecure_channel + P4RuntimeStub + create_subprocess_exec."""
    channel = MagicMock(name="aio-channel")
    channel.close = AsyncMock(return_value=None)
    stub = MagicMock(name="aio-stub")
    stream = _FakeStream()

    def stream_channel(_request_iter: AsyncIterator[Any]) -> _FakeStream:
        return stream

    stub.StreamChannel = MagicMock(side_effect=stream_channel)
    stub.Write = AsyncMock(return_value=MagicMock())
    stub.SetForwardingPipelineConfig = AsyncMock(return_value=MagicMock())
    # `Read` returns an async iterator; default is empty.
    stub.Read = MagicMock()

    mocker.patch(
        "p4net.control.async_client.grpc.aio.insecure_channel",
        return_value=channel,
    )
    mocker.patch(
        "p4net.control.async_client.p4runtime_pb2_grpc.P4RuntimeStub",
        return_value=stub,
    )
    return {"channel": channel, "stub": stub, "stream": stream}


async def _connect(
    patched_aio: dict[str, Any],
    *,
    election_id: tuple[int, int] | None = None,
    status_code: int = 0,
    info_index: P4InfoIndex | None = None,
) -> AsyncP4RuntimeClient:
    client = AsyncP4RuntimeClient(
        grpc_address=("127.0.0.1", 50051),
        device_id=0,
        info_index=info_index or _make_p4info_index(),
        thrift_address=("127.0.0.1", 9090),
        election_id=election_id,
    )
    _arb_task = asyncio.create_task(patched_aio["stream"].push_arbitration(code=status_code))
    await client.connect(timeout=2.0)
    await _arb_task
    return client


def _batch_error(codes: list[int], *, details: str = "batch write failed") -> grpc.aio.AioRpcError:
    status = status_pb2.Status(code=code_pb2.UNKNOWN, message=details)
    for code in codes:
        status.details.add().Pack(p4runtime_pb2.Error(canonical_code=code))
    return grpc.aio.AioRpcError(
        grpc.StatusCode.UNKNOWN,
        (),
        (("grpc-status-details-bin", status.SerializeToString()),),
        details,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_connect_then_disconnect_clean(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio)
    assert client.is_connected
    assert client.is_primary
    await client.disconnect()
    assert not client.is_connected


async def test_disconnect_wakes_pending_connect(patched_aio: dict[str, Any]) -> None:
    client = AsyncP4RuntimeClient(grpc_address=("127.0.0.1", 50051), device_id=0)
    task = asyncio.create_task(client.connect(timeout=30.0))
    try:
        await asyncio.wait_for(patched_aio["stream"].read_started.wait(), timeout=2.0)
        await client.disconnect()
        with pytest.raises(ConnectionError, match="closed"):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        assert not client.is_connected
        patched_aio["channel"].close.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("primary", [True, False])
@pytest.mark.parametrize("ending", ["eof", "rpc-error", "exception", "cancelled"])
async def test_stream_end_releases_resources_and_can_reconnect(
    patched_aio: dict[str, Any], mocker: MockerFixture, primary: bool, ending: str
) -> None:
    client = await _connect(
        patched_aio,
        election_id=(1, 0) if primary else (0, 0),
        status_code=code_pb2.OK if primary else code_pb2.ALREADY_EXISTS,
    )
    stream_task = client._stream_task
    assert stream_task is not None
    error: BaseException | None = None
    if ending == "rpc-error":
        error = grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, (), (), "server stopped")
    elif ending == "exception":
        error = RuntimeError("stream failed")
    try:
        if ending == "cancelled":
            stream_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(stream_task), timeout=5.0)
        else:
            await patched_aio["stream"].finish(error)
            await asyncio.wait_for(asyncio.shield(stream_task), timeout=5.0)
        assert not client.is_connected
        assert not client.is_primary
        patched_aio["channel"].close.assert_awaited_once()
        assert patched_aio["stream"]._cancelled
        assert client._channel is None
        assert client._stub is None
        assert client._stream_call is None
        assert client._stream_task is None
        assert client._outgoing is None
        assert client._packet_in_queue is None
        if error is not None:
            assert client._stream_error is error
        with pytest.raises(ConnectionError):
            await client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": "10.0.0.0/24"},
                "MyIngress.set_egress_port",
                {"port": 2},
            )
        patched_aio["stub"].Write.assert_not_awaited()

        retry_channel = MagicMock(name="retry-channel")
        retry_channel.close = AsyncMock(return_value=None)
        retry_stream = _FakeStream()
        mocker.patch(
            "p4net.control.async_client.grpc.aio.insecure_channel", return_value=retry_channel
        )
        patched_aio["stub"].StreamChannel.side_effect = lambda _requests: retry_stream
        await retry_stream.push_arbitration(code=code_pb2.OK)
        await client.connect()
        assert client.is_connected
        assert client.is_primary
        assert client._stream_error is None
        assert client._stream_task is not stream_task
        await client.disconnect()
        retry_channel.close.assert_awaited_once()
        patched_aio["channel"].close.assert_awaited_once()
    finally:
        await client.disconnect()


@pytest.mark.parametrize("arbitrated", [False, True])
async def test_stream_end_during_connect_fails_promptly(
    patched_aio: dict[str, Any], arbitrated: bool
) -> None:
    client = AsyncP4RuntimeClient(("127.0.0.1", 50051), device_id=0, election_id=(1, 0))
    if arbitrated:
        await patched_aio["stream"].push_arbitration()
    await patched_aio["stream"].finish()
    try:
        with pytest.raises(ConnectionError, match="stream ended"):
            await asyncio.wait_for(client.connect(timeout=60.0), timeout=5.0)
        assert not client.is_connected
        assert not client.is_primary
        patched_aio["channel"].close.assert_awaited_once()
    finally:
        await client.disconnect()


@pytest.mark.parametrize("operation", ["disconnect", "reconnect"])
async def test_lifecycle_waits_for_stream_cleanup(
    patched_aio: dict[str, Any], mocker: MockerFixture, operation: str
) -> None:
    client = await _connect(patched_aio)
    stream_task = client._stream_task
    assert stream_task is not None
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    operation_started = asyncio.Event()
    next_task: asyncio.Task[None] | None = None

    async def close() -> None:
        close_started.set()
        await release_close.wait()

    patched_aio["channel"].close.side_effect = close
    retry_channel = MagicMock(name="retry-channel")
    retry_channel.close = AsyncMock(return_value=None)
    retry_stream = _FakeStream()

    async def run_operation() -> None:
        operation_started.set()
        if operation == "reconnect":
            await client.connect()
        else:
            await client.disconnect()

    try:
        await patched_aio["stream"].finish()
        await asyncio.wait_for(close_started.wait(), timeout=5.0)
        assert not client.is_connected
        assert not client.is_primary
        mocker.patch(
            "p4net.control.async_client.grpc.aio.insecure_channel", return_value=retry_channel
        )
        patched_aio["stub"].StreamChannel.side_effect = lambda _requests: retry_stream
        await retry_stream.push_arbitration()
        next_task = asyncio.create_task(run_operation())
        await asyncio.wait_for(operation_started.wait(), timeout=5.0)
        assert not next_task.done()
        assert patched_aio["stub"].StreamChannel.call_count == 1
        release_close.set()
        await asyncio.wait_for(asyncio.gather(stream_task, next_task), timeout=5.0)
        patched_aio["channel"].close.assert_awaited_once()
        if operation == "reconnect":
            assert client.is_connected
            assert client.is_primary
            assert client._channel is retry_channel
        else:
            assert not client.is_connected
            assert client._channel is None
    finally:
        release_close.set()
        if next_task is not None:
            await asyncio.gather(next_task, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("rpc_error", [True, False])
async def test_stream_error_during_connect_preserves_cause(
    patched_aio: dict[str, Any], rpc_error: bool
) -> None:
    client = AsyncP4RuntimeClient(("127.0.0.1", 50051), device_id=0, election_id=(1, 0))
    error = (
        grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, (), (), "server stopped")
        if rpc_error
        else RuntimeError("stream failed")
    )
    await patched_aio["stream"].finish(error)
    try:
        with pytest.raises(P4RuntimeError, match="async stream error") as raised:
            await asyncio.wait_for(client.connect(timeout=60.0), timeout=5.0)
        assert raised.value.__cause__ is error
        assert not client.is_connected
        assert not client.is_primary
        patched_aio["channel"].close.assert_awaited_once()
    finally:
        await client.disconnect()


@pytest.mark.parametrize("cancel_mode", ["task", "wait-for", "context-manager"])
async def test_cancelled_connect_releases_resources_and_can_retry(
    patched_aio: dict[str, Any], mocker: MockerFixture, cancel_mode: str
) -> None:
    client = AsyncP4RuntimeClient(
        grpc_address=("127.0.0.1", 50051),
        device_id=0,
        info_index=_make_p4info_index(),
    )

    async def enter_context() -> None:
        async with client:
            pytest.fail("context entered before arbitration completed")

    attempt = asyncio.create_task(
        enter_context() if cancel_mode == "context-manager" else client.connect()
    )
    try:
        await asyncio.wait_for(patched_aio["stream"].read_started.wait(), timeout=5.0)
        stream_task = client._stream_task
        assert stream_task is not None
        if cancel_mode == "wait-for":
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(attempt, timeout=0)
        else:
            attempt.cancel("connection cancelled")
            # Python 3.11 added cancellation message propagation to awaiters.
            message = "connection cancelled" if sys.version_info >= (3, 11) else None
            with pytest.raises(asyncio.CancelledError, match=message):
                await attempt

        assert attempt.cancelled()
        patched_aio["channel"].close.assert_awaited_once()
        assert patched_aio["stream"]._cancelled
        assert stream_task.done()
        assert not client.is_connected
        assert not client.is_primary
        assert client._channel is None
        assert client._stub is None
        assert client._stream_call is None
        assert client._stream_task is None
        assert client._outgoing is None
        assert client._packet_in_queue is None

        retry_channel = MagicMock(name="retry-channel")
        retry_channel.close = AsyncMock(return_value=None)
        retry_stream = _FakeStream()
        mocker.patch(
            "p4net.control.async_client.grpc.aio.insecure_channel", return_value=retry_channel
        )
        patched_aio["stub"].StreamChannel.side_effect = lambda _requests: retry_stream
        await retry_stream.push_arbitration()
        await client.connect(timeout=2.0)
        assert client.is_connected
        assert client.is_primary
        assert client._channel is retry_channel
        assert client._stream_task is not stream_task
        await client.disconnect()
        retry_channel.close.assert_awaited_once()
        patched_aio["channel"].close.assert_awaited_once()
    finally:
        attempt.cancel()
        await asyncio.gather(attempt, return_exceptions=True)
        await client.disconnect()


async def test_connect_election_id_auto(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio)
    high, low = client.election_id
    assert high > 0  # ms since epoch is well above zero
    assert low == 0
    await client.disconnect()


async def test_connect_arbitration_timeout_releases_resources(patched_aio: dict[str, Any]) -> None:
    client = AsyncP4RuntimeClient(grpc_address=("127.0.0.1", 50051), device_id=0)
    try:
        with pytest.raises(ConnectionError, match="arbitration timed out"):
            await client.connect(timeout=0)
        patched_aio["channel"].close.assert_awaited_once()
        assert patched_aio["stream"]._cancelled
        assert not client.is_connected
        assert client._channel is None
        assert client._stream_task is None
    finally:
        await client.disconnect()


async def test_explicit_secondary_stays_connected(patched_aio: dict[str, Any]) -> None:
    # status_code != 0 with election_id=(0,0) is an explicit secondary — allowed.
    client = await _connect(patched_aio, election_id=(0, 0), status_code=6)
    assert client.is_connected
    assert not client.is_primary
    await client.disconnect()


@pytest.mark.parametrize("initial_code", [code_pb2.OK, code_pb2.ALREADY_EXISTS])
async def test_primary_state_tracks_arbitration_updates(
    patched_aio: dict[str, Any], initial_code: int
) -> None:
    client = await _connect(patched_aio, election_id=(0, 0), status_code=initial_code)
    states: asyncio.Queue[bool] = asyncio.Queue()

    async def observe_state(_payload: bytes, _metadata: dict[str, int]) -> None:
        states.put_nowait(client.is_primary)

    try:
        await client.on_packet_in(observe_state)
        assert client.is_primary == (initial_code == code_pb2.OK)
        for code in (
            code_pb2.ALREADY_EXISTS,
            code_pb2.NOT_FOUND,
            code_pb2.OK,
            code_pb2.OK,
            code_pb2.ALREADY_EXISTS,
            code_pb2.OK,
        ):
            await patched_aio["stream"].push_arbitration(code=code)
            # PacketIn follows arbitration on the same stream, so its handler
            # must observe the updated state without reconnecting.
            await patched_aio["stream"].push_packet(b"state-check")
            assert await asyncio.wait_for(states.get(), timeout=5.0) == (code == code_pb2.OK)
            assert client.is_connected
        assert patched_aio["stub"].StreamChannel.call_count == 1
        patched_aio["channel"].close.assert_not_awaited()
    finally:
        await client.disconnect()
    assert not client.is_primary


@pytest.mark.parametrize("code", [code_pb2.ALREADY_EXISTS, code_pb2.NOT_FOUND])
async def test_connect_rejects_non_primary(patched_aio: dict[str, Any], code: int) -> None:
    await patched_aio["stream"].push_arbitration(code=code)
    client = AsyncP4RuntimeClient(("127.0.0.1", 50051), device_id=0, election_id=(1, 0))
    try:
        with pytest.raises(NotPrimaryError):
            await client.connect()
        assert not client.is_connected
        assert not client.is_primary
        patched_aio["channel"].close.assert_awaited_once()
    finally:
        await client.disconnect()


async def test_async_context_manager(patched_aio: dict[str, Any]) -> None:
    client = AsyncP4RuntimeClient(
        grpc_address=("127.0.0.1", 50051),
        device_id=0,
        info_index=_make_p4info_index(),
        thrift_address=("127.0.0.1", 9090),
    )
    _arb_task = asyncio.create_task(patched_aio["stream"].push_arbitration())
    async with client as c:
        assert c.is_connected
    await _arb_task
    assert not client.is_connected


# ---------------------------------------------------------------------------
# Pipeline and RPC errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("empty_details", [False, True])
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (grpc.StatusCode.PERMISSION_DENIED, NotPrimaryError),
        (grpc.StatusCode.UNAVAILABLE, ConnectionError),
        (grpc.StatusCode.CANCELLED, AsyncOperationCancelledError),
        (grpc.StatusCode.INVALID_ARGUMENT, PipelineError),
        (grpc.StatusCode.FAILED_PRECONDITION, PipelineError),
        (grpc.StatusCode.NOT_FOUND, EntryNotFoundError),
        (grpc.StatusCode.ALREADY_EXISTS, DuplicateEntryError),
        (grpc.StatusCode.DEADLINE_EXCEEDED, P4RuntimeError),
        (grpc.StatusCode.UNKNOWN, P4RuntimeError),
        (grpc.StatusCode.UNIMPLEMENTED, P4RuntimeError),
        (grpc.StatusCode.INTERNAL, P4RuntimeError),
    ],
)
async def test_push_pipeline_preserves_rpc_error_type_and_index(
    patched_aio: dict[str, Any],
    code: grpc.StatusCode,
    expected: type[P4RuntimeError],
    empty_details: bool,
) -> None:
    client = await _connect(patched_aio)
    details = "" if empty_details else "pipeline request failed"
    error = grpc.aio.AioRpcError(code, (), (), details)
    rpc = patched_aio["stub"].SetForwardingPipelineConfig
    rpc.side_effect = error
    index = client.info_index
    channel = client._channel
    stream = client._stream_call
    try:
        with pytest.raises(expected) as raised:
            await client.push_pipeline(b"", b"{}")
        assert type(raised.value) is expected
        assert raised.value.__cause__ is error
        if details:
            assert details in str(raised.value)
        assert client.info_index is index
        assert client.is_connected
        assert client._channel is channel
        assert client._stream_call is stream
        patched_aio["channel"].close.assert_not_awaited()

        rpc.side_effect = None
        await client.push_pipeline(b"", b"{}")
        assert client.info_index is not index
        assert client.info_index.table_names == []
    finally:
        await client.disconnect()


@pytest.mark.parametrize("ending", ["cancel", "timeout"])
async def test_push_pipeline_cancel_or_timeout_preserves_index(
    patched_aio: dict[str, Any], ending: str
) -> None:
    client = await _connect(patched_aio)
    index = client.info_index
    started = asyncio.Event()

    async def hold_response(request: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    rpc = patched_aio["stub"].SetForwardingPipelineConfig
    rpc.side_effect = hold_response
    task = asyncio.create_task(
        client.push_pipeline(b"", b"{}", timeout=0.05 if ending == "timeout" else 30.0)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        if ending == "cancel":
            task.cancel()
        with pytest.raises(
            AsyncOperationCancelledError if ending == "cancel" else asyncio.TimeoutError
        ):
            await task
        assert client.info_index is index
        assert client.is_connected
        rpc.side_effect = None
        await client.push_pipeline(b"", b"{}")
        assert client.info_index is not index
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("operation", ["write", "read"])
async def test_unavailable_rpc_raises_connection_error(
    patched_aio: dict[str, Any], operation: str
) -> None:
    client = await _connect(patched_aio)
    error = grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, (), (), "device unavailable")
    if operation == "write":
        patched_aio["stub"].Write.side_effect = error
    else:
        patched_aio["stub"].Read.side_effect = error
    try:
        with pytest.raises(ConnectionError, match="device unavailable") as raised:
            if operation == "write":
                await client.insert_table_entry(
                    "MyIngress.ipv4_lpm",
                    {"hdr.ipv4.dstAddr": "10.0.1.0/24"},
                    "MyIngress.set_egress_port",
                    {"port": 2},
                )
            else:
                _ = [entry async for entry in client.list_table_entries()]
        assert raised.value.__cause__ is error
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# Table CRUD
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("empty_details", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize(
    "operation", ["insert", "modify", "delete", "list", "counter-cell", "counter-all"]
)
async def test_failed_precondition_does_not_imply_lost_primary(
    patched_aio: dict[str, Any], operation: str, wrapped: bool, empty_details: bool
) -> None:
    info = _make_p4info_index().raw
    counter = info.counters.add()
    counter.preamble.id = 0x12000001
    counter.preamble.name = "MyIngress.test_counter"
    counter.spec.unit = p4info_pb2.CounterSpec.PACKETS
    counter.size = 16
    client = await _connect(patched_aio, info_index=P4InfoIndex(info))
    details = "" if empty_details else "forwarding pipeline is not configured"
    error = (
        _batch_error([code_pb2.OK, code_pb2.FAILED_PRECONDITION], details=details)
        if wrapped
        else grpc.aio.AioRpcError(grpc.StatusCode.FAILED_PRECONDITION, (), (), details)
    )
    reading = operation in ("list", "counter-cell", "counter-all")
    rpc = patched_aio["stub"].Read if reading else patched_aio["stub"].Write
    rpc.side_effect = error

    async def perform() -> Any:
        if operation == "list":
            return [entry async for entry in client.list_table_entries()]
        if operation.startswith("counter-"):
            return await client.read_counter(
                "MyIngress.test_counter", 0 if operation == "counter-cell" else None
            )
        if operation == "delete":
            return await client.delete_table_entry(
                "MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.1.0/24"}
            )
        method = client.insert_table_entry if operation == "insert" else client.modify_table_entry
        return await method(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.1.0/24"},
            "MyIngress.set_egress_port",
            {"port": 2},
        )

    index, channel, stream = client.info_index, client._channel, client._stream_call
    try:
        with pytest.raises(P4RuntimeError) as raised:
            await perform()
        assert type(raised.value) is P4RuntimeError
        assert raised.value.__cause__ is error
        assert str(raised.value)
        if details:
            assert details in str(raised.value)
        assert client.is_connected and client.is_primary
        assert client.info_index is index
        assert client._channel is channel and client._stream_call is stream
        patched_aio["channel"].close.assert_not_awaited()

        async def empty_read() -> AsyncIterator[Any]:
            yield p4runtime_pb2.ReadResponse()

        rpc.side_effect = None
        if reading:
            rpc.return_value = empty_read()
        await perform()
    finally:
        await client.disconnect()


@pytest.mark.parametrize("empty_details", [False, True])
@pytest.mark.parametrize(
    ("operation", "canonical", "expected"),
    [
        ("insert", code_pb2.ALREADY_EXISTS, DuplicateEntryError),
        ("modify", code_pb2.NOT_FOUND, EntryNotFoundError),
        ("delete", code_pb2.NOT_FOUND, EntryNotFoundError),
        ("insert", code_pb2.PERMISSION_DENIED, NotPrimaryError),
        ("modify", code_pb2.PERMISSION_DENIED, NotPrimaryError),
        ("delete", code_pb2.PERMISSION_DENIED, NotPrimaryError),
    ],
)
async def test_write_decodes_batch_error(
    patched_aio: dict[str, Any],
    operation: str,
    canonical: int,
    expected: type[P4RuntimeError],
    empty_details: bool,
) -> None:
    client = await _connect(patched_aio)
    error = _batch_error(
        [code_pb2.OK, canonical], details="" if empty_details else "batch write failed"
    )
    rpc = patched_aio["stub"].Write
    rpc.side_effect = error
    index, channel, stream = client.info_index, client._channel, client._stream_call
    try:
        with pytest.raises(expected) as raised:
            if operation == "delete":
                await client.delete_table_entry(
                    "MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.1.0/24"}
                )
            else:
                method = (
                    client.insert_table_entry
                    if operation == "insert"
                    else client.modify_table_entry
                )
                await method(
                    "MyIngress.ipv4_lpm",
                    {"hdr.ipv4.dstAddr": "10.0.1.0/24"},
                    "MyIngress.set_egress_port",
                    {"port": 2},
                )
        assert type(raised.value) is expected
        assert raised.value.__cause__ is error
        assert str(raised.value)
        if not empty_details:
            assert "batch write failed" in str(raised.value)
        assert client.info_index is index
        assert client.is_connected
        assert client._channel is channel and client._stream_call is stream
        rpc.side_effect = None
        await client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.2.0/24"},
            "MyIngress.set_egress_port",
            {"port": 1},
        )
    finally:
        await client.disconnect()


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([code_pb2.OK, code_pb2.ALREADY_EXISTS, code_pb2.NOT_FOUND], DuplicateEntryError),
        ([code_pb2.OK, code_pb2.NOT_FOUND, code_pb2.ALREADY_EXISTS], EntryNotFoundError),
        ([999, code_pb2.OK, code_pb2.ALREADY_EXISTS], DuplicateEntryError),
        ([code_pb2.OK, code_pb2.UNAVAILABLE], ConnectionError),
        ([code_pb2.OK, code_pb2.CANCELLED], AsyncOperationCancelledError),
        ([code_pb2.OK, code_pb2.UNKNOWN, code_pb2.ALREADY_EXISTS], P4RuntimeError),
    ],
)
async def test_batch_error_uses_first_recognized_failure(
    patched_aio: dict[str, Any], codes: list[int], expected: type[P4RuntimeError]
) -> None:
    client = await _connect(patched_aio)
    error = _batch_error(codes)
    patched_aio["stub"].Write.side_effect = error
    try:
        with pytest.raises(expected) as raised:
            await client.delete_table_entry(
                "MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.1.0/24"}
            )
        assert type(raised.value) is expected
        assert raised.value.__cause__ is error
    finally:
        await client.disconnect()


@pytest.mark.parametrize(
    "payload", ["missing", "corrupt", "unrelated", "all-ok", "unknown-code", "bad-detail"]
)
async def test_invalid_batch_details_preserve_unknown_error(
    patched_aio: dict[str, Any], payload: str
) -> None:
    status = status_pb2.Status(code=code_pb2.UNKNOWN)
    if payload == "unrelated":
        status.details.add().Pack(p4info_pb2.P4Info())
    elif payload == "all-ok":
        status.details.add().Pack(p4runtime_pb2.Error(canonical_code=code_pb2.OK))
    elif payload == "unknown-code":
        status.details.add().Pack(p4runtime_pb2.Error(canonical_code=999))
    elif payload == "bad-detail":
        status.details.add().Pack(p4runtime_pb2.Error())
        status.details[0].value = b"\xff"
    encoded = b"\xff" if payload == "corrupt" else status.SerializeToString()
    metadata = () if payload == "missing" else (("grpc-status-details-bin", encoded),)
    error = grpc.aio.AioRpcError(grpc.StatusCode.UNKNOWN, (), metadata, "batch write failed")
    client = await _connect(patched_aio)
    patched_aio["stub"].Write.side_effect = error
    try:
        with pytest.raises(P4RuntimeError, match="batch write failed") as raised:
            await client.delete_table_entry(
                "MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.1.0/24"}
            )
        assert type(raised.value) is P4RuntimeError
        assert raised.value.__cause__ is error
    finally:
        await client.disconnect()


async def test_explicit_rpc_status_takes_precedence_over_batch_details(
    patched_aio: dict[str, Any],
) -> None:
    batch = _batch_error([code_pb2.ALREADY_EXISTS])
    error = grpc.aio.AioRpcError(
        grpc.StatusCode.PERMISSION_DENIED, (), batch.trailing_metadata(), "not primary"
    )
    client = await _connect(patched_aio)
    patched_aio["stub"].Write.side_effect = error
    try:
        with pytest.raises(NotPrimaryError) as raised:
            await client.delete_table_entry(
                "MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.1.0/24"}
            )
        assert raised.value.__cause__ is error
    finally:
        await client.disconnect()


async def test_insert_table_entry_builds_write_request(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    try:
        await client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.0.0/24"},
            "MyIngress.set_egress_port",
            {"port": 2},
        )
        req = patched_aio["stub"].Write.call_args.args[0]
        assert req.device_id == 0
        assert len(req.updates) == 1
        upd = req.updates[0]
        assert upd.type == p4runtime_pb2.Update.Type.INSERT
        te = upd.entity.table_entry
        assert te.table_id == 2001
        assert te.action.action.action_id == 1001
    finally:
        await client.disconnect()


async def test_delete_table_entry_uses_delete_type(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    try:
        await client.delete_table_entry("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.0.0/24"})
        req = patched_aio["stub"].Write.call_args.args[0]
        assert req.updates[0].type == p4runtime_pb2.Update.Type.DELETE
    finally:
        await client.disconnect()


@pytest.mark.parametrize("operation", ["insert", "modify", "delete"])
@pytest.mark.parametrize("wildcard", [False, True])
@pytest.mark.parametrize("priority", [1, 17, (1 << 31) - 1])
async def test_optional_table_write_preserves_priority(
    patched_aio: dict[str, Any], operation: str, wildcard: bool, priority: int
) -> None:
    client = await _connect(patched_aio)
    method = getattr(client, f"{operation}_table_entry")
    match = {} if wildcard else {"hdr.ethernet.etherType": 0x0800}
    try:
        if operation == "delete":
            await method("MyIngress.optional_acl", match, priority=priority)
        else:
            await method(
                "MyIngress.optional_acl",
                match,
                "MyIngress.set_egress_port",
                {"port": 2},
                priority=priority,
            )
        rpc = patched_aio["stub"].Write
        rpc.assert_awaited_once()
        update = rpc.call_args.args[0].updates[0]
        assert update.type == p4runtime_pb2.Update.Type.Value(operation.upper())
        entry = update.entity.table_entry
        assert entry.table_id == 2002
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
        await client.disconnect()


@pytest.mark.parametrize("operation", ["insert", "modify"])
@pytest.mark.parametrize("wildcard", [False, True])
async def test_optional_table_missing_priority_does_not_write(
    patched_aio: dict[str, Any], operation: str, wildcard: bool
) -> None:
    client = await _connect(patched_aio)
    method = getattr(client, f"{operation}_table_entry")
    match = {} if wildcard else {"hdr.ethernet.etherType": 0x0800}
    try:
        with pytest.raises(EncodingError, match=r"optional.*priority required"):
            await method("MyIngress.optional_acl", match, "MyIngress.set_egress_port", {"port": 2})
        patched_aio["stub"].Write.assert_not_awaited()
    finally:
        await client.disconnect()


async def test_list_table_entries_yields_decoded(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    try:

        async def _async_iter() -> AsyncIterator[Any]:
            resp = p4runtime_pb2.ReadResponse()
            ent = resp.entities.add()
            ent.table_entry.table_id = 2001
            fm = ent.table_entry.match.add()
            fm.field_id = 1
            fm.lpm.value = b"\x0a\x00\x00\x00"
            fm.lpm.prefix_len = 24
            ent.table_entry.action.action.action_id = 1001
            yield resp

        patched_aio["stub"].Read = MagicMock(return_value=_async_iter())
        entries: list[dict[str, Any]] = []
        async for e in client.list_table_entries("MyIngress.ipv4_lpm"):
            entries.append(e)
        assert len(entries) == 1
        assert entries[0]["table"] == "MyIngress.ipv4_lpm"
        assert entries[0]["action"] == "MyIngress.set_egress_port"
    finally:
        await client.disconnect()


async def test_reads_across_clients_progress_concurrently() -> None:
    started: set[int] = set()
    all_started = asyncio.Event()
    release_responses = asyncio.Event()

    async def read(request: Any, **kwargs: Any) -> AsyncIterator[Any]:
        started.add(request.device_id)
        if len(started) == 3:
            all_started.set()
        await release_responses.wait()
        response = p4runtime_pb2.ReadResponse()
        entry = response.entities.add().table_entry
        entry.table_id = 2001
        entry.action.action.action_id = 1001
        param = entry.action.action.params.add()
        param.param_id = 1
        param.value = bytes([request.device_id + 1])
        yield response

    clients = []
    for device_id in range(3):
        client = AsyncP4RuntimeClient(
            grpc_address=("127.0.0.1", 50051 + device_id),
            device_id=device_id,
            info_index=_make_p4info_index(),
        )
        client._connected = True
        client._stub = MagicMock(Read=MagicMock(side_effect=read))
        clients.append(client)

    async def list_entries(client: AsyncP4RuntimeClient) -> list[dict[str, Any]]:
        return [entry async for entry in client.list_table_entries("MyIngress.ipv4_lpm")]

    tasks = [asyncio.create_task(list_entries(client)) for client in clients]
    try:
        await asyncio.wait_for(all_started.wait(), timeout=5.0)
        assert started == {0, 1, 2}
        assert all(not task.done() for task in tasks)
        release_responses.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)
        assert [len(entries) for entries in results] == [1, 1, 1]
        assert [entries[0]["params"]["port"] for entries in results] == [b"\x01", b"\x02", b"\x03"]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(client.disconnect() for client in clients))


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


async def test_read_counter_single_index(patched_aio: dict[str, Any]) -> None:
    # Add a counter to the P4Info first.
    p = p4info_pb2.P4Info()
    c = p.counters.add()
    c.preamble.id = 5001
    c.preamble.name = "MyIngress.pkts"
    client = await _connect(patched_aio, info_index=P4InfoIndex(p))
    try:

        async def _async_iter() -> AsyncIterator[Any]:
            resp = p4runtime_pb2.ReadResponse()
            ent = resp.entities.add()
            ent.counter_entry.counter_id = 5001
            ent.counter_entry.index.index = 3
            ent.counter_entry.data.packet_count = 42
            yield resp

        patched_aio["stub"].Read = MagicMock(return_value=_async_iter())
        out = await client.read_counter("MyIngress.pkts", index=3)
        assert out == 42
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# Packet I/O
# ---------------------------------------------------------------------------


async def test_send_packet_out_enqueues_request(patched_aio: dict[str, Any]) -> None:
    # Use a P4Info with no packet_out controller header (default).
    client = await _connect(patched_aio)
    try:
        await client.send_packet_out(b"hello world")
        # The outgoing queue should now contain the request. Drain it.
        out = client._outgoing
        assert out is not None
        # First message was the arbitration; we sent the packet after.
        # Drain at least one StreamMessageRequest with a packet field.
        seen_packet = False
        while not out.empty():
            msg = out.get_nowait()
            if msg is None:
                break
            if msg.HasField("packet"):
                assert msg.packet.payload == b"hello world"
                seen_packet = True
                break
        assert seen_packet
    finally:
        await client.disconnect()


async def test_on_packet_in_handler_invoked(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio)
    received: list[tuple[bytes, dict[str, int]]] = []

    async def handler(payload: bytes, meta: dict[str, int]) -> None:
        received.append((payload, meta))

    try:
        await client.on_packet_in(handler)
        # Inject a packet via the fake stream.
        await patched_aio["stream"].push_packet(b"PKT")
        # Allow the consumer task a moment to dispatch.
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.05)
        assert received and received[0][0] == b"PKT"
    finally:
        await client.disconnect()


async def test_expect_packet_in_timeout(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio)
    try:
        with pytest.raises(P4RuntimeError, match="no PacketIn within"):
            await client.expect_packet_in(timeout=0.2)
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# Registers (mocked subprocess)
# ---------------------------------------------------------------------------


async def test_write_register_index_out_of_range(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio)
    try:
        with pytest.raises(EncodingError, match=r"out of range \[0, 16\)"):
            await client.write_register("MyIngress.test_register", index=99, value=1)
    finally:
        await client.disconnect()


async def test_write_register_value_exceeds_bitwidth(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    try:
        with pytest.raises(EncodingError, match="does not fit in 32 bits"):
            await client.write_register("MyIngress.test_register", index=0, value=2**32)
    finally:
        await client.disconnect()


async def test_read_register_unknown_raises(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio)
    try:
        with pytest.raises(NoSuchRegisterError):
            await client.read_register("missing")
    finally:
        await client.disconnect()


async def test_write_register_shells_out_to_thrift_cli(
    patched_aio: dict[str, Any],
    mocker: MockerFixture,
) -> None:
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"RuntimeCmd:\n", b""))
    mocker.patch(
        "p4net.control.async_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    )
    mocker.patch(
        "p4net.control.async_client.shutil.which", return_value="/usr/local/bin/simple_switch_CLI"
    )
    client = await _connect(patched_aio)
    try:
        await client.write_register("MyIngress.test_register", index=3, value=51966)
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_insert_cancelled_raises_async_operation_cancelled(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    try:

        async def _never() -> Any:
            await asyncio.sleep(60)

        patched_aio["stub"].Write = MagicMock(side_effect=lambda req: _never())
        task = asyncio.create_task(
            client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": "10.0.0.0/24"},
                "MyIngress.set_egress_port",
                {"port": 2},
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(AsyncOperationCancelledError):
            await task
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# Pre-connect failures
# ---------------------------------------------------------------------------


async def test_methods_require_connection(patched_aio: dict[str, Any]) -> None:
    client = AsyncP4RuntimeClient(
        grpc_address=("127.0.0.1", 50051),
        device_id=0,
        info_index=_make_p4info_index(),
    )
    with pytest.raises(ConnectionError):
        await client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.0.0/24"},
            "MyIngress.set_egress_port",
            {"port": 1},
        )


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
async def test_delete_priority_table_validates_before_rpc(
    patched_aio: dict[str, Any],
    match_type: int,
    value: object,
    wildcard: bool,
    priority: int | None,
) -> None:
    client = await _connect(patched_aio)
    table = next(t for t in client.info_index.raw.tables if t.preamble.name == "MyIngress.ipv4_lpm")
    table.match_fields[0].match_type = match_type
    match = {} if wildcard else {"hdr.ipv4.dstAddr": value}
    try:
        if priority is None:
            with pytest.raises(EncodingError, match="priority"):
                await client.delete_table_entry("MyIngress.ipv4_lpm", match)
            patched_aio["stub"].Write.assert_not_awaited()
        else:
            await client.delete_table_entry("MyIngress.ipv4_lpm", match, priority=priority)
            patched_aio["stub"].Write.assert_awaited_once()
            update = patched_aio["stub"].Write.call_args.args[0].updates[0]
            assert update.type == p4runtime_pb2.Update.Type.DELETE
            entry = update.entity.table_entry
            assert entry.table_id == 2001
            assert entry.priority == priority
            assert len(entry.match) == (0 if wildcard else 1)
            assert not entry.HasField("action")
    finally:
        await client.disconnect()


async def test_delete_exact_table_does_not_require_priority(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    table = next(t for t in client.info_index.raw.tables if t.preamble.name == "MyIngress.ipv4_lpm")
    table.match_fields[0].match_type = p4info_pb2.MatchField.EXACT
    try:
        await client.delete_table_entry("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.0.1"})
        patched_aio["stub"].Write.assert_awaited_once()
        entry = patched_aio["stub"].Write.call_args.args[0].updates[0].entity.table_entry
        assert entry.priority == 0
        assert entry.match[0].HasField("exact")
    finally:
        await client.disconnect()


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
async def test_invalid_priority_does_not_write(
    patched_aio: dict[str, Any], operation: str, priority: Any
) -> None:
    client = await _connect(patched_aio)
    method = getattr(client, f"{operation}_table_entry")
    match = {"hdr.ethernet.etherType": 0x0800}
    try:
        with pytest.raises(EncodingError, match="priority"):
            if operation == "delete":
                await method("MyIngress.optional_acl", match, priority=priority)
            else:
                await method(
                    "MyIngress.optional_acl",
                    match,
                    "MyIngress.set_egress_port",
                    {"port": 2},
                    priority=priority,
                )
        patched_aio["stub"].Write.assert_not_awaited()
    finally:
        await client.disconnect()


@pytest.mark.parametrize("operation", ["insert", "modify", "delete"])
@pytest.mark.parametrize("match_type", [p4info_pb2.MatchField.EXACT, p4info_pb2.MatchField.LPM])
@pytest.mark.parametrize("priority", [0, False])
async def test_exact_lpm_priority_must_be_none(
    patched_aio: dict[str, Any], operation: str, match_type: int, priority: int
) -> None:
    client = await _connect(patched_aio)
    table = next(t for t in client.info_index.raw.tables if t.preamble.name == "MyIngress.ipv4_lpm")
    table.match_fields[0].match_type = match_type
    value = "10.0.0.1" if match_type == p4info_pb2.MatchField.EXACT else "10.0.0.0/24"
    match = {"hdr.ipv4.dstAddr": value}
    method = getattr(client, f"{operation}_table_entry")
    try:
        with pytest.raises(EncodingError, match="priority must be None"):
            if operation == "delete":
                await method("MyIngress.ipv4_lpm", match, priority=priority)
            else:
                await method(
                    "MyIngress.ipv4_lpm",
                    match,
                    "MyIngress.set_egress_port",
                    {"port": 2},
                    priority=priority,
                )
        patched_aio["stub"].Write.assert_not_awaited()
    finally:
        await client.disconnect()
