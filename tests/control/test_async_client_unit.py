"""Unit tests for ``p4net.control.AsyncP4RuntimeClient``. All gRPC mocked."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
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
    P4RuntimeClient,
    P4RuntimeError,
    PipelineError,
)
from p4net.network.nodes import RunningSwitch
from p4net.topo import P4Switch

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
    stub.GetForwardingPipelineConfig = AsyncMock(return_value=_pipeline_reply())
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
    index = info_index or _make_p4info_index()
    client = AsyncP4RuntimeClient(
        grpc_address=("127.0.0.1", 50051),
        device_id=0,
        info_index=index,
        thrift_address=("127.0.0.1", 9090),
        election_id=election_id,
    )
    patched_aio["stub"].GetForwardingPipelineConfig.return_value = _pipeline_reply(index)
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


def _lifecycle_client(info: P4InfoIndex, *, managed: bool) -> AsyncP4RuntimeClient:
    if not managed:
        return AsyncP4RuntimeClient(("127.0.0.1", 50051), device_id=3, info_index=info)
    sync_client = P4RuntimeClient("127.0.0.1:50051", device_id=3)
    sync_client._index = info
    switch = RunningSwitch(
        P4Switch(name="s1", p4_src=Path("routing.p4")),
        MagicMock(device_id=3, grpc_port=50051, thrift_port=9090),
        sync_client,
        MagicMock(),
    )
    return switch.async_client


def _pipeline_reply(info: P4InfoIndex | None = None) -> Any:
    reply = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    if info is not None:
        reply.config.p4info.CopyFrom(info.raw)
    return reply


async def _connect_paired_clients(
    patched_aio: dict[str, Any],
) -> tuple[P4RuntimeClient, AsyncP4RuntimeClient]:
    sync = P4RuntimeClient("127.0.0.1:50051", device_id=0)
    sync._connected = True
    sync._stub = MagicMock()
    sync._index = _make_p4info_index()
    switch = RunningSwitch(
        P4Switch(name="s1", p4_src=Path("routing.p4")),
        MagicMock(device_id=0, grpc_port=50051, thrift_port=9090),
        sync,
        MagicMock(),
    )
    client = switch.async_client
    patched_aio["stub"].GetForwardingPipelineConfig.return_value = _pipeline_reply(sync.index)
    await patched_aio["stream"].push_arbitration()
    await client.connect()
    assert sync.index is client.info_index
    return sync, client


def _renamed_pipeline(index: P4InfoIndex) -> P4InfoIndex:
    info = p4info_pb2.P4Info()
    info.CopyFrom(index.raw)
    info.tables[0].preamble.name = "NextIngress.ipv4_lpm"
    return P4InfoIndex(info)


@pytest.mark.parametrize("outcome", ["replace", "commit", "verify", "rejected", "timeout"])
async def test_paired_clients_share_async_pipeline_outcomes(
    patched_aio: dict[str, Any], outcome: str
) -> None:
    sync, client = await _connect_paired_clients(patched_aio)
    previous = sync.index
    updated = _renamed_pipeline(previous)
    stub = patched_aio["stub"]
    files = (str(updated.raw).encode(), b"{}")
    try:
        if outcome == "commit":
            stub.GetForwardingPipelineConfig.return_value = _pipeline_reply(updated)
            await client.push_pipeline(action="COMMIT")
        elif outcome == "verify":
            await client.push_pipeline(*files, action="VERIFY")
        elif outcome == "rejected":
            stub.SetForwardingPipelineConfig.side_effect = grpc.aio.AioRpcError(
                grpc.StatusCode.PERMISSION_DENIED, (), (), "not primary"
            )
            with pytest.raises(NotPrimaryError, match="not primary"):
                await client.push_pipeline(*files)
        elif outcome == "timeout":

            async def hold_reply(_request: Any) -> None:
                await asyncio.Future()

            stub.SetForwardingPipelineConfig.side_effect = hold_reply
            with pytest.raises(asyncio.TimeoutError):
                await client.push_pipeline(*files, timeout=0.05)
        else:
            await client.push_pipeline(*files)

        if outcome == "timeout":
            with pytest.raises(P4RuntimeError, match="no pipeline is set"):
                sync.list_table_entries()
            with pytest.raises(P4RuntimeError, match="no P4Info index"):
                _ = client.info_index
            sync._stub.Read.assert_not_called()
        else:
            expected = updated if outcome in ("replace", "commit") else previous
            assert sync.index is client.info_index
            assert sync.index.raw == expected.raw
            response = p4runtime_pb2.ReadResponse()
            response.entities.add().table_entry.table_id = 2001
            sync._stub.Read.return_value = iter([response])
            table = expected.table_names[0]
            assert [entry["table"] for entry in sync.list_table_entries(table)] == [table]
    finally:
        sync.disconnect()
        await client.disconnect()


async def test_paired_read_cannot_pass_an_async_pipeline_change(
    patched_aio: dict[str, Any],
) -> None:
    sync, client = await _connect_paired_clients(patched_aio)
    updated = _renamed_pipeline(client.info_index)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_reply(_request: Any) -> None:
        entered.set()
        await release.wait()

    patched_aio["stub"].SetForwardingPipelineConfig.side_effect = hold_reply
    changing = asyncio.create_task(client.push_pipeline(str(updated.raw).encode(), b"{}"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        with pytest.raises(P4RuntimeError, match="pipeline change is in progress"):
            sync.list_table_entries()
        sync._stub.Read.assert_not_called()
        release.set()
        await asyncio.wait_for(changing, timeout=1.0)
        assert sync.index.raw == updated.raw
    finally:
        release.set()
        await asyncio.gather(changing, return_exceptions=True)
        sync.disconnect()
        await client.disconnect()


async def test_pipeline_change_can_retry_after_a_paired_read(patched_aio: dict[str, Any]) -> None:
    sync, client = await _connect_paired_clients(patched_aio)
    updated = _renamed_pipeline(client.info_index)
    entered, release = threading.Event(), threading.Event()

    def hold_read(_request: Any, *, timeout: float) -> Iterator[Any]:
        entered.set()
        assert release.wait(timeout=3.0)
        return iter(())

    sync._stub.Read.side_effect = hold_read
    reading = asyncio.create_task(asyncio.to_thread(sync.list_table_entries))
    try:
        assert await asyncio.to_thread(entered.wait, 1.0)
        with pytest.raises(P4RuntimeError, match="operations are using P4Info"):
            await client.push_pipeline(str(updated.raw).encode(), b"{}")
        patched_aio["stub"].SetForwardingPipelineConfig.assert_not_called()
        release.set()
        assert await asyncio.wait_for(reading, timeout=1.0) == []
        await client.push_pipeline(str(updated.raw).encode(), b"{}")
        assert sync.index.raw == updated.raw
    finally:
        release.set()
        await asyncio.gather(reading, return_exceptions=True)
        sync.disconnect()
        await client.disconnect()


async def test_paired_client_reads_sync_pipeline_and_preserves_it_after_failed_reconnect(
    patched_aio: dict[str, Any], mocker: MockerFixture, tmp_path: Path
) -> None:
    sync, client = await _connect_paired_clients(patched_aio)
    updated = _renamed_pipeline(client.info_index)
    p4info, bmv2_json = tmp_path / "pipeline.txtpb", tmp_path / "pipeline.json"
    p4info.write_text(str(updated.raw))
    bmv2_json.write_text("{}")
    try:
        sync.set_pipeline_config(bmv2_json=bmv2_json, p4info=p4info)
        index = sync.index
        assert client.info_index is index and index.raw == updated.raw
        await client.disconnect()
        stub, stream = _replace_async_stream(mocker)
        stub.GetForwardingPipelineConfig = AsyncMock(
            side_effect=grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, (), (), "server stopped")
        )
        await stream.push_arbitration()
        with pytest.raises(ConnectionError, match="server stopped"):
            await client.connect()
        with pytest.raises(P4RuntimeError, match="no P4Info index"):
            _ = client.info_index
        assert sync.index is index
    finally:
        sync.disconnect()
        await client.disconnect()


def _replace_async_stream(mocker: MockerFixture) -> tuple[Any, _FakeStream]:
    stream = _FakeStream()
    stub = MagicMock(StreamChannel=MagicMock(return_value=stream))
    mocker.patch("p4net.control.async_client.p4runtime_pb2_grpc.P4RuntimeStub", return_value=stub)
    return stub, stream


async def _connect_then_replace_stream(
    client: AsyncP4RuntimeClient,
    patched_aio: dict[str, Any],
    mocker: MockerFixture,
    *,
    managed: bool,
) -> tuple[Any, _FakeStream]:
    reply = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    reply.config.p4info.CopyFrom(client.info_index.raw)
    initial_stub = patched_aio["stub"]
    initial_stub.GetForwardingPipelineConfig = AsyncMock(return_value=reply)
    await patched_aio["stream"].push_arbitration()
    try:
        await client.connect()
        assert initial_stub.GetForwardingPipelineConfig.await_count == int(managed)
    finally:
        await client.disconnect()
    return _replace_async_stream(mocker)


@pytest.mark.parametrize("p4info", ["same", "changed", "missing", "unconfigured-bmv2"])
@pytest.mark.parametrize("connection", ["managed-first", "managed-reconnect", "direct-reconnect"])
async def test_connection_confirms_p4info_when_readback_is_required(
    patched_aio: dict[str, Any], mocker: MockerFixture, p4info: str, connection: str
) -> None:
    previous = _make_p4info_index()
    managed = connection.startswith("managed")
    client = _lifecycle_client(previous, managed=managed)
    assert client.info_index is previous
    stub, stream = patched_aio["stub"], patched_aio["stream"]
    if connection.endswith("reconnect"):
        stub, stream = await _connect_then_replace_stream(
            client, patched_aio, mocker, managed=managed
        )
    reply = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    if p4info in ("same", "changed"):
        reply.config.p4info.CopyFrom(previous.raw)
        if p4info == "changed":
            reply.config.p4info.tables[0].preamble.name = "NextIngress.ipv4_lpm"
    read_started, release = asyncio.Event(), asyncio.Event()

    async def read_pipeline(request: Any) -> Any:
        read_started.set()
        await release.wait()
        if p4info == "unconfigured-bmv2":
            raise grpc.aio.AioRpcError(
                grpc.StatusCode.FAILED_PRECONDITION,
                (),
                (),
                "No forwarding pipeline config set for this device",
            )
        return reply

    stub.GetForwardingPipelineConfig = AsyncMock(side_effect=read_pipeline)
    connecting = asyncio.create_task(client.connect())
    waiting = asyncio.create_task(client.connect())
    try:
        await stream.push_arbitration()
        await asyncio.wait_for(read_started.wait(), timeout=1.0)
        assert not client.is_connected
        assert not waiting.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(connecting, waiting), timeout=1.0)
        assert client.is_connected
        request = stub.GetForwardingPipelineConfig.await_args.args[0]
        assert request.device_id == 3
        assert request.response_type == request.P4INFO_AND_COOKIE
        await client.connect()
        stub.GetForwardingPipelineConfig.assert_awaited_once()
        if p4info in ("missing", "unconfigured-bmv2"):
            with pytest.raises(P4RuntimeError, match="no P4Info index"):
                _ = client.info_index
        else:
            assert client.info_index.raw == reply.config.p4info
            assert (client.info_index is previous) == (p4info == "same")
    finally:
        release.set()
        await asyncio.gather(connecting, waiting, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("ending", ["disconnect", "timeout", "cancel", "rpc-error"])
@pytest.mark.parametrize("connection", ["managed-first", "managed-reconnect", "direct-reconnect"])
async def test_failed_p4info_readback_is_retried_on_next_connect(
    patched_aio: dict[str, Any], mocker: MockerFixture, ending: str, connection: str
) -> None:
    managed = connection.startswith("managed")
    client = _lifecycle_client(_make_p4info_index(), managed=managed)
    stub, stream = patched_aio["stub"], patched_aio["stream"]
    if connection.endswith("reconnect"):
        stub, stream = await _connect_then_replace_stream(
            client, patched_aio, mocker, managed=managed
        )
    read_started, release = asyncio.Event(), asyncio.Event()
    reply = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    reply.config.p4info.CopyFrom(_make_p4info_index().raw)
    reply.config.p4info.tables[0].preamble.name = "NextIngress.ipv4_lpm"

    async def hold_pipeline(_request: Any) -> Any:
        read_started.set()
        await release.wait()
        if ending == "rpc-error":
            raise grpc.aio.AioRpcError(
                grpc.StatusCode.FAILED_PRECONDITION, (), (), "pipeline state unavailable"
            )
        return reply

    stub.GetForwardingPipelineConfig = AsyncMock(side_effect=hold_pipeline)
    await stream.push_arbitration()
    pending = asyncio.create_task(client.connect(timeout=0.1 if ending == "timeout" else 3.0))
    try:
        await asyncio.wait_for(read_started.wait(), timeout=1.0)
        if ending == "disconnect":
            await client.disconnect()
            release.set()
            with pytest.raises(ConnectionError, match="closed during P4Info readback"):
                await asyncio.wait_for(pending, timeout=1.0)
        elif ending == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        elif ending == "rpc-error":
            release.set()
            with pytest.raises(P4RuntimeError, match="pipeline state unavailable") as raised:
                await pending
            assert type(raised.value) is P4RuntimeError
        else:
            with pytest.raises(ConnectionError, match="P4Info readback timed out"):
                await pending
        assert not client.is_connected
        assert client._channel is None
        with pytest.raises(P4RuntimeError, match="no P4Info index"):
            _ = client.info_index

        retry_stub, retry_stream = _replace_async_stream(mocker)
        retry_stub.GetForwardingPipelineConfig = AsyncMock(return_value=reply)
        await retry_stream.push_arbitration()
        await client.connect()
        assert client.info_index.raw == reply.config.p4info
        retry_stub.GetForwardingPipelineConfig.assert_awaited_once()
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("managed", [True, False])
async def test_reconnect_waits_for_an_older_pipeline_rpc(
    patched_aio: dict[str, Any], mocker: MockerFixture, managed: bool
) -> None:
    previous = _make_p4info_index()
    client = _lifecycle_client(previous, managed=managed)
    old_stub = patched_aio["stub"]
    reply = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    reply.config.p4info.CopyFrom(previous.raw)
    old_stub.GetForwardingPipelineConfig = AsyncMock(return_value=reply)
    await patched_aio["stream"].push_arbitration()
    await client.connect()

    applying, release = asyncio.Event(), asyncio.Event()
    reply.config.p4info.tables[0].preamble.name = "NextIngress.ipv4_lpm"

    async def hold_set(_request: Any) -> None:
        applying.set()
        await release.wait()

    old_stub.SetForwardingPipelineConfig.side_effect = hold_set
    updating = asyncio.create_task(client.push_pipeline(str(reply.config.p4info).encode(), b"{}"))
    reconnecting = None
    try:
        await asyncio.wait_for(applying.wait(), timeout=1.0)
        await client.disconnect()
        new_stub, new_stream = _replace_async_stream(mocker)
        new_stub.GetForwardingPipelineConfig = AsyncMock(return_value=reply)
        await new_stream.push_arbitration()
        reconnecting = asyncio.create_task(client.connect())
        done, _ = await asyncio.wait((reconnecting,), timeout=0.02)
        assert not done
        new_stub.GetForwardingPipelineConfig.assert_not_called()
        assert not client.is_connected

        release.set()
        with pytest.raises(ConnectionError, match="closed"):
            await asyncio.wait_for(updating, timeout=1.0)
        await asyncio.wait_for(reconnecting, timeout=1.0)
        assert client.is_connected and client.info_index.raw == reply.config.p4info
        new_stub.GetForwardingPipelineConfig.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(updating, return_exceptions=True)
        if reconnecting is not None:
            await asyncio.gather(reconnecting, return_exceptions=True)
        await client.disconnect()


async def test_connect_then_disconnect_clean(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio)
    assert client.is_connected
    assert client.is_primary
    await client.disconnect()
    assert not client.is_connected


@pytest.mark.parametrize("configured", [False, True])
async def test_direct_client_starts_with_the_constructor_index(
    patched_aio: dict[str, Any], configured: bool
) -> None:
    initial = _make_p4info_index() if configured else None
    client = AsyncP4RuntimeClient(("127.0.0.1", 50051), device_id=0, info_index=initial)
    try:
        await patched_aio["stream"].push_arbitration()
        await client.connect()
        assert client.is_connected
        patched_aio["stub"].GetForwardingPipelineConfig.assert_not_called()
        if configured:
            assert client.info_index is initial
        else:
            with pytest.raises(P4RuntimeError, match="no P4Info index"):
                _ = client.info_index
    finally:
        await client.disconnect()


@pytest.mark.parametrize("step", ["stub", "stream"])
async def test_connect_initialization_failure_closes_channel_and_allows_retry(
    patched_aio: dict[str, Any], mocker: MockerFixture, step: str
) -> None:
    client = AsyncP4RuntimeClient(("127.0.0.1", 50051), device_id=0)
    failed_channel = patched_aio["channel"]
    retry_channel = MagicMock(name="retry-channel", close=AsyncMock(return_value=None))
    mocker.patch(
        "p4net.control.async_client.grpc.aio.insecure_channel",
        side_effect=[failed_channel, retry_channel],
    )
    stub = patched_aio["stub"]
    stub_factory = mocker.patch(
        "p4net.control.async_client.p4runtime_pb2_grpc.P4RuntimeStub", return_value=stub
    )
    failing_call = stub_factory if step == "stub" else stub.StreamChannel
    original_side_effect = failing_call.side_effect
    failure = RuntimeError(f"{step} initialization failed")
    failing_call.side_effect = failure

    try:
        with pytest.raises(RuntimeError) as raised:
            async with client:
                pytest.fail("connection unexpectedly succeeded")
        assert raised.value is failure
        failed_channel.close.assert_awaited_once()
        assert client._channel is None
        assert client._stream_task is None
        assert not client.is_connected

        failing_call.side_effect = original_side_effect
        await patched_aio["stream"].push_arbitration()
        async with client:
            assert client.is_connected and client.is_primary
        retry_channel.close.assert_awaited_once()
        failed_channel.close.assert_awaited_once()
    finally:
        await client.disconnect()


async def test_connect_invalid_arbitration_request_releases_resources(
    patched_aio: dict[str, Any],
) -> None:
    client = AsyncP4RuntimeClient(("127.0.0.1", 50051), device_id=-1)
    try:
        with pytest.raises(ValueError, match="out of range"):
            await client.connect()
        assert not client.is_connected
        assert patched_aio["stream"]._cancelled
        patched_aio["channel"].close.assert_awaited_once()
        assert client._channel is None
        assert client._stream_task is None
    finally:
        await client.disconnect()


@pytest.mark.parametrize("outcome", ["success", "stream-error", "disconnect", "cancel"])
async def test_concurrent_connects_share_one_attempt(
    patched_aio: dict[str, Any], outcome: str
) -> None:
    client = AsyncP4RuntimeClient(("127.0.0.1", 50051), device_id=0)
    stream = patched_aio["stream"]
    attempts = [asyncio.create_task(client.connect(timeout=2.0))]
    try:
        await asyncio.wait_for(stream.read_started.wait(), timeout=1.0)
        attempts.append(asyncio.create_task(client.connect(timeout=0.0)))
        await asyncio.sleep(0)
        error = grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, (), (), "server stopped")
        if outcome == "success":
            await stream.push_arbitration()
        elif outcome == "stream-error":
            await stream.finish(error)
        elif outcome == "disconnect":
            await client.disconnect()
        else:
            attempts[0].cancel()

        results = await asyncio.wait_for(asyncio.gather(*attempts, return_exceptions=True), 3.0)
        patched_aio["stub"].StreamChannel.assert_called_once()
        if outcome == "success":
            assert results == [None, None]
        else:
            assert not client.is_connected
            patched_aio["channel"].close.assert_awaited_once()
            if outcome == "stream-error":
                assert all(
                    isinstance(result, P4RuntimeError) and result.__cause__ is error
                    for result in results
                )
            elif outcome == "disconnect":
                assert all(isinstance(result, ConnectionError) for result in results)
            else:
                assert all(isinstance(result, asyncio.CancelledError) for result in results)

            retry_stream = _FakeStream()
            patched_aio["stub"].StreamChannel.side_effect = lambda _requests: retry_stream
            await retry_stream.push_arbitration()
            await client.connect(timeout=1.0)
            assert patched_aio["stub"].StreamChannel.call_count == 2
        assert client.is_connected and client.is_primary
    finally:
        for attempt in attempts:
            attempt.cancel()
        await asyncio.gather(*attempts, return_exceptions=True)
        await client.disconnect()


async def test_cancelling_connect_waiter_keeps_active_connection_attempt(
    patched_aio: dict[str, Any],
) -> None:
    client = AsyncP4RuntimeClient(("127.0.0.1", 50051), device_id=0)
    stream = patched_aio["stream"]
    attempt = asyncio.create_task(client.connect(timeout=2.0))
    waiter = None
    try:
        await asyncio.wait_for(stream.read_started.wait(), timeout=1.0)
        waiter = asyncio.create_task(client.connect())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not attempt.done()
        await stream.push_arbitration()
        await asyncio.wait_for(attempt, timeout=1.0)
        patched_aio["stub"].StreamChannel.assert_called_once()
        assert client.is_connected
    finally:
        attempt.cancel()
        await asyncio.gather(attempt, return_exceptions=True)
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await client.disconnect()


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


@pytest.mark.parametrize("ending", ["eof", "rpc-error", "late-response"])
async def test_old_stream_cannot_close_reconnected_client(
    patched_aio: dict[str, Any], mocker: MockerFixture, ending: str
) -> None:
    client = await _connect(patched_aio)
    old_stream = patched_aio["stream"]
    old_task = client._stream_task
    assert old_task is not None
    # Let the old response iterator return after the callback has finished.
    old_cancel = mocker.patch.object(old_stream, "cancel")
    disconnected = asyncio.Event()
    release_handler = asyncio.Event()
    seen: list[bytes] = []

    async def disconnect_from_handler(payload: bytes, _metadata: dict[str, int]) -> None:
        if payload == b"old connection":
            await client.disconnect()
            disconnected.set()
            await release_handler.wait()

    async def record(payload: bytes, _metadata: dict[str, int]) -> None:
        seen.append(payload)

    await client.on_packet_in(disconnect_from_handler)
    await client.on_packet_in(record)
    retry_channel = MagicMock(name="retry-channel", close=AsyncMock(return_value=None))
    retry_stream = _FakeStream()
    mocker.patch("p4net.control.async_client.grpc.aio.insecure_channel", return_value=retry_channel)
    patched_aio["stub"].StreamChannel.side_effect = lambda _requests: retry_stream
    try:
        await old_stream.push_packet(b"old connection")
        await asyncio.wait_for(disconnected.wait(), timeout=1.0)
        old_cancel.assert_called_once()
        patched_aio["channel"].close.assert_awaited_once()
        await retry_stream.push_arbitration()
        await client.connect(timeout=1.0)

        if ending == "late-response":
            await old_stream.push_arbitration(code=code_pb2.ALREADY_EXISTS)
            await old_stream.push_packet(b"old response after reconnect")
        error = grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, (), (), "old stream stopped")
        await old_stream.finish(error if ending == "rpc-error" else None)
        release_handler.set()
        await asyncio.wait_for(asyncio.shield(old_task), timeout=1.0)

        retry_channel.close.assert_not_awaited()
        assert client._channel is retry_channel
        assert client.is_connected and client.is_primary
        assert client._stream_error is None
        await retry_stream.push_packet(b"new connection")
        assert await client.expect_packet_in(timeout=1.0) == (b"new connection", {})
        assert seen == [b"new connection"]
    finally:
        release_handler.set()
        old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)
        await client.disconnect()


async def test_old_request_iterator_cannot_drain_new_connection(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    stream_factory = patched_aio["stub"].StreamChannel
    old_requests = stream_factory.call_args.args[0]
    try:
        assert (await anext(old_requests)).HasField("arbitration")
        await client.disconnect()
        retry_stream = _FakeStream()
        stream_factory.side_effect = lambda _requests: retry_stream
        await retry_stream.push_arbitration()
        await client.connect(timeout=1.0)

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(old_requests), timeout=1.0)
        new_requests = stream_factory.call_args.args[0]
        assert (await asyncio.wait_for(anext(new_requests), timeout=1.0)).HasField("arbitration")
    finally:
        await old_requests.aclose()
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


@pytest.mark.parametrize("delegated", [False, True])
async def test_packet_handler_can_disconnect_during_cancellation(
    patched_aio: dict[str, Any], delegated: bool
) -> None:
    client = await _connect(patched_aio)
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def handle(_payload: bytes, _metadata: dict[str, int]) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            if delegated:
                await asyncio.wait_for(client.disconnect(), timeout=3.0)
            else:
                await client.disconnect()
            cleaned_up.set()

    await client.on_packet_in(handle)
    closing = None
    try:
        await patched_aio["stream"].push_packet(b"packet")
        await asyncio.wait_for(started.wait(), timeout=1.0)
        closing = asyncio.create_task(client.disconnect())
        await asyncio.wait_for(asyncio.shield(closing), timeout=0.5)
        assert cleaned_up.is_set()
        assert not client.is_connected
        patched_aio["channel"].close.assert_awaited_once()
    finally:
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await client.disconnect()


async def test_cancelled_disconnect_finishes_closing_the_channel(
    patched_aio: dict[str, Any], mocker: MockerFixture
) -> None:
    client = await _connect(patched_aio)
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def close() -> None:
        close_started.set()
        await release_close.wait()

    patched_aio["channel"].close.side_effect = close
    first = asyncio.create_task(client.disconnect())
    second = None
    try:
        await asyncio.wait_for(close_started.wait(), timeout=1.0)
        second = asyncio.create_task(client.disconnect())
        for _ in range(2):
            first.cancel()
            await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(second, timeout=1.0)
        assert not client.is_connected
        patched_aio["channel"].close.assert_awaited_once()

        retry_channel = MagicMock(name="retry-channel", close=AsyncMock(return_value=None))
        mocker.patch(
            "p4net.control.async_client.grpc.aio.insecure_channel", return_value=retry_channel
        )
        retry_stream = _FakeStream()
        patched_aio["stub"].StreamChannel.side_effect = lambda _requests: retry_stream
        await retry_stream.push_arbitration()
        await client.connect(timeout=1.0)
        assert client.is_connected and client.is_primary
        retry_channel.close.assert_not_awaited()
    finally:
        release_close.set()
        await asyncio.gather(first, return_exceptions=True)
        if second is not None:
            await asyncio.gather(second, return_exceptions=True)
        await client.disconnect()


async def test_cancelled_handler_cleanup_does_not_close_a_reconnection(
    patched_aio: dict[str, Any], mocker: MockerFixture
) -> None:
    client = await _connect(patched_aio)
    handler_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def handle(_payload: bytes, _metadata: dict[str, int]) -> None:
        handler_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            await client.disconnect()

    await client.on_packet_in(handle)
    closing = None
    try:
        await patched_aio["stream"].push_packet(b"packet")
        await asyncio.wait_for(handler_started.wait(), timeout=1.0)
        closing = asyncio.create_task(client.disconnect())
        await asyncio.sleep(0)
        assert not client.is_connected
        await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)

        retry_channel = MagicMock(name="retry-channel", close=AsyncMock(return_value=None))
        mocker.patch(
            "p4net.control.async_client.grpc.aio.insecure_channel", return_value=retry_channel
        )
        retry_stream = _FakeStream()
        patched_aio["stub"].StreamChannel.side_effect = lambda _requests: retry_stream
        await retry_stream.push_arbitration()
        await asyncio.wait_for(client.connect(), timeout=0.5)
        release_cleanup.set()
        await asyncio.wait_for(closing, timeout=0.5)

        assert client.is_connected and client.is_primary
        assert client._channel is retry_channel
        retry_channel.close.assert_not_awaited()
        patched_aio["channel"].close.assert_awaited_once()
    finally:
        release_cleanup.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
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


@pytest.mark.parametrize("installed", [False, True])
async def test_verify_pipeline_keeps_the_current_index(
    patched_aio: dict[str, Any], installed: bool
) -> None:
    client = AsyncP4RuntimeClient(("127.0.0.1", 50051), device_id=0)
    rpc = patched_aio["stub"].SetForwardingPipelineConfig
    active = _make_p4info_index().raw
    candidate = p4info_pb2.P4Info()
    candidate.CopyFrom(active)
    candidate.tables[0].preamble.name = "Candidate.ipv4_lpm"
    await patched_aio["stream"].push_arbitration()
    try:
        await client.connect()
        if installed:
            await client.push_pipeline(str(active).encode(), b"{}")
        index = client.info_index if installed else None

        await client.push_pipeline(str(candidate).encode(), b"{}", action="VERIFY")

        sent = rpc.await_args.args[0]
        assert sent.action == p4runtime_pb2.SetForwardingPipelineConfigRequest.VERIFY
        assert sent.config.p4info == candidate
        assert sent.config.p4_device_config == b"{}"
        if installed:
            assert client.info_index is index
        else:
            with pytest.raises(P4RuntimeError, match="no P4Info index"):
                _ = client.info_index
    finally:
        await client.disconnect()


@pytest.mark.parametrize("index_state", ["saved", "unknown", "older"])
async def test_commit_uses_the_switch_p4info(patched_aio: dict[str, Any], index_state: str) -> None:
    client = AsyncP4RuntimeClient(("127.0.0.1", 50051), device_id=0)
    stub = patched_aio["stub"]
    active = _make_p4info_index().raw
    candidate = p4info_pb2.P4Info()
    candidate.CopyFrom(active)
    candidate.tables[0].preamble.name = "Candidate.ipv4_lpm"
    response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    response.config.p4info.CopyFrom(candidate)
    stub.GetForwardingPipelineConfig = AsyncMock(return_value=response)
    await patched_aio["stream"].push_arbitration()
    try:
        await client.connect()
        saved = None
        if index_state != "unknown":
            p4info = candidate if index_state == "saved" else active
            await client.push_pipeline(str(p4info).encode(), b"{}", action="VERIFY_AND_SAVE")
            saved = client.info_index

        if index_state == "older":
            await client.push_pipeline(b"\xff", b"ignored", action="COMMIT", timeout=2.0)
        else:
            await client.push_pipeline(action="COMMIT", timeout=2.0)

        sent = stub.SetForwardingPipelineConfig.await_args.args[0]
        assert sent.action == p4runtime_pb2.SetForwardingPipelineConfigRequest.COMMIT
        assert not sent.HasField("config")
        sent = stub.GetForwardingPipelineConfig.await_args.args[0]
        assert (
            sent.response_type == p4runtime_pb2.GetForwardingPipelineConfigRequest.P4INFO_AND_COOKIE
        )
        assert client.info_index.raw == candidate
        if index_state == "saved":
            assert client.info_index is saved
    finally:
        await client.disconnect()


@pytest.mark.parametrize("failure", ["commit", "refresh", "missing-p4info", "cancel"])
async def test_commit_failure_leaves_a_usable_or_empty_index(
    patched_aio: dict[str, Any], failure: str
) -> None:
    client = await _connect(patched_aio)
    saved = client.info_index
    stub = patched_aio["stub"]
    stub.GetForwardingPipelineConfig = AsyncMock(
        return_value=p4runtime_pb2.GetForwardingPipelineConfigResponse()
    )
    if failure == "commit":
        stub.SetForwardingPipelineConfig.side_effect = grpc.aio.AioRpcError(
            grpc.StatusCode.NOT_FOUND, (), (), "no saved pipeline"
        )
    elif failure == "refresh":
        stub.GetForwardingPipelineConfig.side_effect = grpc.aio.AioRpcError(
            grpc.StatusCode.UNAVAILABLE, (), (), "could not get configuration"
        )
    elif failure == "cancel":
        stub.GetForwardingPipelineConfig.side_effect = asyncio.CancelledError

    try:
        message = "no saved pipeline" if failure == "commit" else "COMMIT succeeded"
        error = AsyncOperationCancelledError if failure == "cancel" else P4RuntimeError
        with pytest.raises(error, match=message):
            await client.push_pipeline(action="COMMIT")
        if failure == "commit":
            assert client.info_index is saved
            stub.GetForwardingPipelineConfig.assert_not_awaited()
        else:
            with pytest.raises(P4RuntimeError, match="no P4Info index"):
                _ = client.info_index
    finally:
        await client.disconnect()


@pytest.mark.parametrize("reconnect", [False, True])
async def test_commit_refresh_cannot_overwrite_a_later_pipeline(
    patched_aio: dict[str, Any], mocker: MockerFixture, reconnect: bool
) -> None:
    client = await _connect(patched_aio)
    old_stub = patched_aio["stub"]
    newer_stub = old_stub
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    response.config.p4info.CopyFrom(client.info_index.raw)

    async def hold_read(_request: Any) -> Any:
        read_started.set()
        await release_read.wait()
        return response

    old_stub.GetForwardingPipelineConfig = AsyncMock(side_effect=hold_read)
    commit = asyncio.create_task(client.push_pipeline(action="COMMIT"))
    obsolete = None
    newer = None
    reconnecting = None
    try:
        await asyncio.wait_for(read_started.wait(), timeout=1.0)
        if reconnect:
            obsolete = asyncio.create_task(client.push_pipeline(b"", b"{}"))
            await asyncio.sleep(0)
            await client.disconnect()
            newer_stub, stream = _replace_async_stream(mocker)
            newer_stub.SetForwardingPipelineConfig = AsyncMock()
            newer_stub.GetForwardingPipelineConfig = AsyncMock(return_value=response)
            await stream.push_arbitration()
            reconnecting = asyncio.create_task(client.connect())
            done, _ = await asyncio.wait((reconnecting,), timeout=0.02)
            assert not done
            newer_stub.GetForwardingPipelineConfig.assert_not_called()
            old_stub.SetForwardingPipelineConfig.assert_awaited_once()

            release_read.set()
            with pytest.raises(ConnectionError, match="COMMIT succeeded"):
                await asyncio.wait_for(commit, timeout=1.0)
            with pytest.raises(ConnectionError, match="closed before pipeline RPC"):
                await asyncio.wait_for(obsolete, timeout=1.0)
            await asyncio.wait_for(reconnecting, timeout=1.0)

        next_info = _make_p4info_index().raw
        next_info.tables[0].preamble.name = "Newer.ipv4_lpm"
        newer = asyncio.create_task(client.push_pipeline(str(next_info).encode(), b"{}"))
        await asyncio.sleep(0)
        assert not newer.done()
        assert old_stub.SetForwardingPipelineConfig.call_count == 1
        if reconnect:
            newer_stub.SetForwardingPipelineConfig.assert_not_called()

        if not reconnect:
            release_read.set()
            await commit
        await newer
        assert newer_stub.SetForwardingPipelineConfig.call_count == (1 if reconnect else 2)
        assert client.info_index.raw == next_info
    finally:
        release_read.set()
        await asyncio.gather(commit, return_exceptions=True)
        if obsolete is not None:
            await asyncio.gather(obsolete, return_exceptions=True)
        if reconnecting is not None:
            await asyncio.gather(reconnecting, return_exceptions=True)
        if newer is not None:
            await asyncio.gather(newer, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("action", ["VERIFY_AND_COMMIT", "VERIFY", "COMMIT"])
async def test_pipeline_reply_from_an_old_connection_does_not_install_the_candidate(
    patched_aio: dict[str, Any], mocker: MockerFixture, action: str
) -> None:
    client = await _connect(patched_aio)
    current = client.info_index
    candidate = p4info_pb2.P4Info()
    candidate.CopyFrom(current.raw)
    candidate.tables[0].preamble.name = "Candidate.ipv4_lpm"
    p4info = str(candidate).encode()
    started = asyncio.Event()
    reply = asyncio.Event()

    async def hold_reply(_request: Any) -> None:
        started.set()
        await reply.wait()

    old_stub = patched_aio["stub"]
    old_stub.SetForwardingPipelineConfig.side_effect = hold_reply
    config = () if action == "COMMIT" else (p4info, b"{}")
    pending = asyncio.create_task(client.push_pipeline(*config, action=action))
    reconnecting = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await client.disconnect()
        mocker.patch(
            "p4net.control.async_client.grpc.aio.insecure_channel",
            return_value=MagicMock(close=AsyncMock()),
        )
        new_stub, stream = _replace_async_stream(mocker)
        new_stub.SetForwardingPipelineConfig = AsyncMock()
        new_stub.GetForwardingPipelineConfig = AsyncMock(return_value=_pipeline_reply(current))
        await stream.push_arbitration()
        reconnecting = asyncio.create_task(client.connect())
        done, _ = await asyncio.wait((reconnecting,), timeout=0.02)
        assert not done
        new_stub.GetForwardingPipelineConfig.assert_not_called()
        reply.set()

        with pytest.raises(ConnectionError, match="closed"):
            await asyncio.wait_for(pending, timeout=1.0)
        await asyncio.wait_for(reconnecting, timeout=1.0)
        assert client.info_index.raw == current.raw
        assert (client.info_index is current) == (action == "VERIFY")
        old_stub.GetForwardingPipelineConfig.assert_not_called()
        new_stub.GetForwardingPipelineConfig.assert_awaited_once()
        new_stub.SetForwardingPipelineConfig.assert_not_awaited()

        await client.push_pipeline(p4info, b"{}")
        assert client.info_index.raw == candidate
        new_stub.SetForwardingPipelineConfig.assert_awaited_once()
    finally:
        reply.set()
        await asyncio.gather(pending, return_exceptions=True)
        if reconnecting is not None:
            await asyncio.gather(reconnecting, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("empty_details", [False, True])
@pytest.mark.parametrize(
    ("code", "expected", "clears_index"),
    [
        (grpc.StatusCode.PERMISSION_DENIED, NotPrimaryError, False),
        (grpc.StatusCode.UNAVAILABLE, ConnectionError, True),
        (grpc.StatusCode.CANCELLED, AsyncOperationCancelledError, True),
        (grpc.StatusCode.INVALID_ARGUMENT, PipelineError, False),
        (grpc.StatusCode.FAILED_PRECONDITION, PipelineError, False),
        (grpc.StatusCode.NOT_FOUND, EntryNotFoundError, False),
        (grpc.StatusCode.ALREADY_EXISTS, DuplicateEntryError, False),
        (grpc.StatusCode.DEADLINE_EXCEEDED, P4RuntimeError, True),
        (grpc.StatusCode.UNKNOWN, P4RuntimeError, True),
        (grpc.StatusCode.UNIMPLEMENTED, P4RuntimeError, False),
        (grpc.StatusCode.INTERNAL, P4RuntimeError, True),
    ],
)
async def test_push_pipeline_preserves_rpc_error_type_and_discards_uncertain_index(
    patched_aio: dict[str, Any],
    code: grpc.StatusCode,
    expected: type[P4RuntimeError],
    clears_index: bool,
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
        if clears_index:
            with pytest.raises(P4RuntimeError, match="no P4Info index"):
                _ = client.info_index
        else:
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
@pytest.mark.parametrize(
    "action", ["VERIFY", "VERIFY_AND_SAVE", "VERIFY_AND_COMMIT", "COMMIT", "RECONCILE_AND_COMMIT"]
)
async def test_push_pipeline_cancel_or_timeout_after_dispatch(
    patched_aio: dict[str, Any], action: str, ending: str
) -> None:
    client = await _connect(patched_aio)
    index = client.info_index
    started = asyncio.Event()

    async def hold_response(request: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    rpc = patched_aio["stub"].SetForwardingPipelineConfig
    rpc.side_effect = hold_response
    files = () if action == "COMMIT" else (b"", b"{}")
    task = asyncio.create_task(
        client.push_pipeline(*files, action=action, timeout=0.1 if ending == "timeout" else 5.0)
    )
    read = None
    table = "MyIngress.ipv4_lpm"
    stub = patched_aio["stub"]
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        iterator = client.list_table_entries(table)
        read = asyncio.create_task(anext(iterator, None))
        await asyncio.sleep(0)
        stub.Read.assert_not_called()
        if ending == "cancel":
            task.cancel()
        with pytest.raises(
            AsyncOperationCancelledError if ending == "cancel" else asyncio.TimeoutError
        ):
            await task
        if action == "VERIFY":
            assert client.info_index is index
            assert await read is None
            stub.Read.assert_called_once()
        else:
            with pytest.raises(P4RuntimeError, match="P4Info changed"):
                await read
            with pytest.raises(P4RuntimeError, match="no P4Info index"):
                client.list_table_entries(table)
            stub.Read.assert_not_called()
            stub.GetForwardingPipelineConfig.assert_not_called()
        assert client.is_connected
        rpc.side_effect = None
        await client.push_pipeline(b"", b"{}")
        assert client.info_index is not index
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if read is not None:
            await asyncio.gather(read, return_exceptions=True)
        await client.disconnect()


async def test_cancelled_pipeline_waiting_for_dispatch_keeps_the_index(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    original = client.info_index
    try:
        async with client._pipeline_lock:
            pending = asyncio.create_task(client.push_pipeline(b"", b"{}"))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(AsyncOperationCancelledError, match="push_pipeline cancelled"):
                await pending
            patched_aio["stub"].SetForwardingPipelineConfig.assert_not_called()
        assert client.info_index is original
    finally:
        await client.disconnect()


@pytest.mark.parametrize("operation", ["table", "pipeline", "commit", "refresh"])
@pytest.mark.parametrize(
    "ending", ["disconnect", "disconnect-status", "caller-and-disconnect", "timeout"]
)
async def test_async_write_rpcs_distinguish_disconnect_from_cancellation(
    patched_aio: dict[str, Any], operation: str, ending: str
) -> None:
    client = await _connect(patched_aio)
    original_index = client.info_index
    stub = patched_aio["stub"]
    waiting, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    transport_error = (
        grpc.aio.AioRpcError(grpc.StatusCode.CANCELLED, (), (), "channel closed")
        if ending == "disconnect-status"
        else asyncio.CancelledError("channel closed")
    )

    async def hold_response(_request: Any) -> None:
        waiting.set()
        try:
            await release.wait()
            raise transport_error
        finally:
            finished.set()

    timeout = 0.15 if ending == "timeout" else 5.0
    if operation == "table":
        stub.Write.side_effect = hold_response
        pending = asyncio.create_task(
            client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": "10.0.0.1/32"},
                "MyIngress.set_egress_port",
                {"port": 2},
                timeout=timeout,
            )
        )
    elif operation == "refresh":
        stub.GetForwardingPipelineConfig = AsyncMock(side_effect=hold_response)
        pending = asyncio.create_task(client.push_pipeline(action="COMMIT", timeout=timeout))
    else:
        stub.SetForwardingPipelineConfig.side_effect = hold_response
        if operation == "commit":
            pending = asyncio.create_task(client.push_pipeline(action="COMMIT", timeout=timeout))
        else:
            pending = asyncio.create_task(client.push_pipeline(b"", b"{}", timeout=timeout))

    try:
        await asyncio.wait_for(waiting.wait(), timeout=1.0)
        if ending != "timeout":
            if ending == "caller-and-disconnect":

                async def close() -> None:
                    pending.cancel()
                    release.set()

                patched_aio["channel"].close.side_effect = close
            await client.disconnect()
            release.set()

        if ending == "timeout":
            with pytest.raises(asyncio.TimeoutError) as raised:
                await asyncio.wait_for(pending, timeout=2.0)
            if operation == "refresh":
                assert "COMMIT succeeded. P4Info refresh timed out." in str(raised.value)
            assert client.is_connected
        elif ending == "caller-and-disconnect":
            with pytest.raises(AsyncOperationCancelledError, match="cancelled") as raised:
                await pending
            assert isinstance(raised.value.__cause__, asyncio.CancelledError)
        else:
            with pytest.raises(ConnectionError) as raised:
                await pending
            assert raised.value.__cause__ is transport_error
        if operation == "refresh":
            assert "COMMIT succeeded." in str(raised.value)
            with pytest.raises(P4RuntimeError, match="no P4Info index"):
                _ = client.info_index
        elif operation == "table":
            assert "COMMIT succeeded." not in str(raised.value)
            assert client.info_index is original_index
            stub.GetForwardingPipelineConfig.assert_not_called()
        else:
            assert "COMMIT succeeded." not in str(raised.value)
            with pytest.raises(P4RuntimeError, match="no P4Info index"):
                _ = client.info_index
            stub.GetForwardingPipelineConfig.assert_not_called()
        assert finished.is_set()
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await client.disconnect()


async def test_table_write_rechecks_p4info_before_dispatch(
    patched_aio: dict[str, Any], mocker: MockerFixture
) -> None:
    client = await _connect(patched_aio)
    lookup = client.info_index.table_id

    def change_p4info_after_encoding(table: str) -> int:
        asyncio.get_running_loop().call_soon(setattr, client, "_info_index", _make_p4info_index())
        return lookup(table)

    mocker.patch.object(client.info_index, "table_id", side_effect=change_p4info_after_encoding)
    try:
        with pytest.raises(P4RuntimeError, match="P4Info changed during table write"):
            await client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": "10.0.0.1/32"},
                "MyIngress.set_egress_port",
                {"port": 2},
            )
        patched_aio["stub"].Write.assert_not_called()
    finally:
        await client.disconnect()


@pytest.mark.parametrize("action", ["VERIFY", "VERIFY_AND_COMMIT", "COMMIT"])
async def test_async_table_write_waits_for_pipeline_and_rechecks_p4info(
    patched_aio: dict[str, Any], mocker: MockerFixture, action: str
) -> None:
    client = await _connect(patched_aio)
    stub = patched_aio["stub"]
    original = client.info_index
    candidate = _make_p4info_index().raw
    candidate.tables[0].preamble.id = 3001
    response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
    response.config.p4info.CopyFrom(candidate)
    pushing, release_push = asyncio.Event(), asyncio.Event()
    refreshing, release_refresh = asyncio.Event(), asyncio.Event()
    prepared = asyncio.Event()
    lookup = original.table_id

    async def hold_push(_request: Any) -> None:
        pushing.set()
        await release_push.wait()

    async def hold_refresh(_request: Any) -> Any:
        refreshing.set()
        await release_refresh.wait()
        return response

    def note_preparation(table: str) -> int:
        prepared.set()
        return lookup(table)

    stub.SetForwardingPipelineConfig.side_effect = hold_push
    stub.GetForwardingPipelineConfig = AsyncMock(side_effect=hold_refresh)
    mocker.patch.object(original, "table_id", side_effect=note_preparation)
    files = () if action == "COMMIT" else (str(candidate).encode(), b"{}")
    pipeline = asyncio.create_task(client.push_pipeline(*files, action=action))
    table_write = None
    table = "MyIngress.ipv4_lpm"
    try:
        await asyncio.wait_for(pushing.wait(), timeout=1.0)
        table_write = asyncio.create_task(
            client.delete_table_entry(table, {"hdr.ipv4.dstAddr": "10.0.0.1/32"})
        )
        await asyncio.wait_for(prepared.wait(), timeout=1.0)
        done, _ = await asyncio.wait((table_write,), timeout=0.02)
        assert not done
        stub.Write.assert_not_called()

        release_push.set()
        if action == "COMMIT":
            await asyncio.wait_for(refreshing.wait(), timeout=1.0)
            assert not table_write.done()
            stub.Write.assert_not_called()
            release_refresh.set()
        await asyncio.wait_for(pipeline, timeout=1.0)

        if action == "VERIFY":
            await asyncio.wait_for(table_write, timeout=1.0)
            assert client.info_index is original
            assert stub.Write.call_args.args[0].updates[0].entity.table_entry.table_id == 2001
        else:
            with pytest.raises(P4RuntimeError, match="P4Info changed during table write"):
                await asyncio.wait_for(table_write, timeout=1.0)
            stub.Write.assert_not_called()
            await client.delete_table_entry(table, {"hdr.ipv4.dstAddr": "10.0.0.1/32"})
            assert stub.Write.call_args.args[0].updates[0].entity.table_entry.table_id == 3001
        stub.Write.assert_awaited_once()
    finally:
        release_push.set()
        release_refresh.set()
        if table_write is not None:
            await asyncio.gather(table_write, return_exceptions=True)
        await asyncio.gather(pipeline, return_exceptions=True)
        await client.disconnect()


async def test_async_pipeline_waits_for_all_inflight_table_writes(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    stub = patched_aio["stub"]
    other = AsyncP4RuntimeClient(
        ("127.0.0.1", 50052), device_id=0, info_index=_make_p4info_index(), election_id=(1, 0)
    )
    other._connected = True
    other._stub = MagicMock(Write=AsyncMock())
    both_writing = asyncio.Event()
    release = (asyncio.Event(), asyncio.Event())
    entered = 0

    async def hold_write(_request: Any) -> None:
        nonlocal entered
        entry = entered
        entered += 1
        if entered == 2:
            both_writing.set()
        await release[entry].wait()

    stub.Write.side_effect = hold_write
    table = "MyIngress.ipv4_lpm"
    writes = [
        asyncio.create_task(
            client.delete_table_entry(table, {"hdr.ipv4.dstAddr": f"10.0.0.{cell}/32"})
        )
        for cell in (1, 2)
    ]
    pipeline = None
    try:
        await asyncio.wait_for(both_writing.wait(), timeout=1.0)
        pipeline = asyncio.create_task(client.push_pipeline(b"", b"{}"))
        await asyncio.wait_for(
            other.delete_table_entry(table, {"hdr.ipv4.dstAddr": "10.0.0.3/32"}), timeout=1.0
        )
        assert not pipeline.done()
        stub.SetForwardingPipelineConfig.assert_not_called()

        release[0].set()
        await asyncio.wait_for(writes[0], timeout=1.0)
        assert not pipeline.done()
        stub.SetForwardingPipelineConfig.assert_not_called()

        release[1].set()
        await asyncio.wait_for(writes[1], timeout=1.0)
        await asyncio.wait_for(pipeline, timeout=1.0)
        stub.SetForwardingPipelineConfig.assert_awaited_once()
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*writes, return_exceptions=True)
        if pipeline is not None:
            await asyncio.gather(pipeline, return_exceptions=True)
        await asyncio.gather(client.disconnect(), other.disconnect())


@pytest.mark.parametrize("ending", ["cancel", "timeout"])
async def test_async_pipeline_proceeds_when_an_inflight_table_write_aborts(
    patched_aio: dict[str, Any], ending: str
) -> None:
    client = await _connect(patched_aio)
    stub = patched_aio["stub"]
    started, finished = asyncio.Event(), asyncio.Event()

    async def hold_write(_request: Any) -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()

    async def push(_request: Any) -> None:
        assert finished.is_set()

    stub.Write.side_effect = hold_write
    stub.SetForwardingPipelineConfig.side_effect = push
    table = "MyIngress.ipv4_lpm"
    writing = asyncio.create_task(
        client.delete_table_entry(
            table,
            {"hdr.ipv4.dstAddr": "10.0.0.1/32"},
            timeout=0.05 if ending == "timeout" else 5.0,
        )
    )
    pipeline = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        pipeline = asyncio.create_task(client.push_pipeline(b"", b"{}"))
        await asyncio.sleep(0)
        if ending == "cancel":
            writing.cancel()
        error = AsyncOperationCancelledError if ending == "cancel" else asyncio.TimeoutError
        with pytest.raises(error):
            await asyncio.wait_for(writing, timeout=1.0)
        await asyncio.wait_for(pipeline, timeout=1.0)
        stub.SetForwardingPipelineConfig.assert_awaited_once()
    finally:
        if not writing.done():
            writing.cancel()
        await asyncio.gather(writing, return_exceptions=True)
        if pipeline is not None:
            await asyncio.gather(pipeline, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("operation", ["table", "pipeline"])
async def test_expired_write_deadline_does_not_dispatch_rpc(
    patched_aio: dict[str, Any], operation: str
) -> None:
    client = await _connect(patched_aio)
    original = client.info_index
    try:
        with pytest.raises(asyncio.TimeoutError):
            if operation == "table":
                await client.delete_table_entry(
                    "MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.0.1/32"}, timeout=0
                )
            else:
                await client.push_pipeline(b"", b"{}", timeout=0)
        stub = patched_aio["stub"]
        stub.Write.assert_not_called()
        stub.SetForwardingPipelineConfig.assert_not_called()
        assert client.info_index is original
    finally:
        await client.disconnect()


async def test_table_write_times_out_while_waiting_for_pipeline(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    stub = patched_aio["stub"]
    table, key = "MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.0.1/32"}
    try:
        async with client._pipeline_lock:
            with pytest.raises(asyncio.TimeoutError):
                await client.delete_table_entry(table, key, timeout=0.02)
            stub.Write.assert_not_called()
        await client.delete_table_entry(table, key)
        stub.Write.assert_awaited_once()
    finally:
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


@pytest.mark.parametrize("reconnect", [False, True])
async def test_unstarted_read_iterator_is_bound_to_its_connection(
    patched_aio: dict[str, Any], mocker: MockerFixture, reconnect: bool
) -> None:
    client = await _connect(patched_aio)
    read = client.list_table_entries("MyIngress.ipv4_lpm")
    old_stub = patched_aio["stub"]
    old_stub.Read.assert_not_called()

    async def respond() -> AsyncIterator[Any]:
        response = p4runtime_pb2.ReadResponse()
        response.entities.add().table_entry.table_id = 2001
        yield response

    retry_stream = _FakeStream()
    retry_stub = MagicMock(StreamChannel=MagicMock(return_value=retry_stream))
    retry_stub.GetForwardingPipelineConfig = AsyncMock(
        return_value=_pipeline_reply(client.info_index)
    )
    retry_stub.Read.side_effect = lambda _request, timeout: respond()
    try:
        await client.disconnect()
        if reconnect:
            mocker.patch(
                "p4net.control.async_client.p4runtime_pb2_grpc.P4RuntimeStub",
                return_value=retry_stub,
            )
            await retry_stream.push_arbitration()
            await client.connect()

        with pytest.raises(ConnectionError, match="closed before reading table entries"):
            await anext(read)
        old_stub.Read.assert_not_called()
        retry_stub.Read.assert_not_called()
        if reconnect:
            fresh_entries = [entry async for entry in client.list_table_entries()]
            assert [entry["table"] for entry in fresh_entries] == ["MyIngress.ipv4_lpm"]
            retry_stub.Read.assert_called_once()
    finally:
        await client.disconnect()


@pytest.mark.parametrize("position", ["before-start", "next-response", "same-response"])
async def test_read_iterator_rejects_a_new_p4info(
    patched_aio: dict[str, Any], position: str
) -> None:
    client = await _connect(patched_aio)
    old_table = "MyIngress.ipv4_lpm"
    new_table = "NextIngress.ipv4_lpm"
    old_action = "MyIngress.set_egress_port"
    new_action = "NextIngress.set_egress_port"
    new_info = p4info_pb2.P4Info()
    new_info.CopyFrom(client.info_index.raw)
    new_info.tables[0].preamble.name = new_table
    new_info.actions[0].preamble.name = new_action

    async def respond() -> AsyncIterator[Any]:
        batch_sizes = (2,) if position == "same-response" else (1, 1)
        for row_count in batch_sizes:
            response = p4runtime_pb2.ReadResponse()
            for _ in range(row_count):
                row = response.entities.add().table_entry
                row.table_id = 2001
                row.action.action.action_id = 1001
            yield response

    rpc = patched_aio["stub"].Read
    rpc.side_effect = lambda _request, timeout: respond()
    read = client.list_table_entries(old_table)
    try:
        started = position != "before-start"
        if started:
            first = await anext(read)
            assert (first["table"], first["action"]) == (old_table, old_action)
        await asyncio.wait_for(client.push_pipeline(str(new_info).encode(), b"{}"), timeout=1.0)

        when = "while" if started else "before"
        with pytest.raises(P4RuntimeError, match=f"P4Info changed {when} reading"):
            await anext(read)
        if started:
            rpc.assert_called_once()
        else:
            rpc.assert_not_called()

        fresh = [entry async for entry in client.list_table_entries(new_table)]
        assert [(entry["table"], entry["action"]) for entry in fresh] == [
            (new_table, new_action)
        ] * 2
    finally:
        await client.disconnect()


@pytest.mark.parametrize("change", ["reconnect", "p4info"])
@pytest.mark.parametrize("reply", ["row", "eof"])
async def test_started_read_iterator_rejects_a_delayed_reply(
    patched_aio: dict[str, Any], mocker: MockerFixture, change: str, reply: str
) -> None:
    client = await _connect(patched_aio)
    original_stub = current_stub = patched_aio["stub"]
    waiting, release = asyncio.Event(), asyncio.Event()
    table = "MyIngress.ipv4_lpm"

    def response(table_id: int) -> Any:
        result = p4runtime_pb2.ReadResponse()
        result.entities.add().table_entry.table_id = table_id
        return result

    active_id = 2001

    async def delayed() -> AsyncIterator[Any]:
        waiting.set()
        await release.wait()
        if reply == "row":
            yield response(2001)

    call = MagicMock(spec=grpc.aio.UnaryStreamCall)
    call.__aiter__.side_effect = delayed
    call.done.return_value = False
    original_stub.Read.return_value = call
    iterator = client.list_table_entries(table)
    pending = asyncio.create_task(anext(iterator))

    try:
        await asyncio.wait_for(waiting.wait(), timeout=1.0)
        if change == "reconnect":
            current_index = client.info_index
            await client.disconnect()
            current_stub, stream = _replace_async_stream(mocker)
            current_stub.GetForwardingPipelineConfig = AsyncMock(
                return_value=_pipeline_reply(current_index)
            )
            await stream.push_arbitration()
            await client.connect()
        else:
            active_id = 4001
            p4info = p4info_pb2.P4Info()
            p4info.CopyFrom(client.info_index.raw)
            p4info.tables[0].preamble.id = active_id
            # Simulate replacing the local index while a response is in flight.
            client._info_index = P4InfoIndex(p4info)
        release.set()

        if change == "reconnect":
            with pytest.raises(ConnectionError, match="closed while reading table entries"):
                await asyncio.wait_for(pending, timeout=1.0)
            current_stub.Read.assert_not_called()
        else:
            with pytest.raises(P4RuntimeError, match="P4Info changed while reading table entries"):
                await asyncio.wait_for(pending, timeout=1.0)
        call.cancel.assert_called_once()
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)

        async def fresh() -> AsyncIterator[Any]:
            yield response(active_id)

        current_stub.Read.return_value = fresh()
        assert [row["table"] async for row in client.list_table_entries(table)] == [table]
        request = current_stub.Read.call_args.args[0]
        assert request.entities[0].table_entry.table_id == active_id
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("ending", ["close", "cancel"])
async def test_ending_table_iteration_cancels_an_unfinished_read(
    patched_aio: dict[str, Any], ending: str
) -> None:
    client = await _connect(patched_aio)
    waiting = asyncio.Event()

    async def responses() -> AsyncIterator[Any]:
        waiting.set()
        if ending == "cancel":
            await asyncio.Future()
        response = p4runtime_pb2.ReadResponse()
        response.entities.add().table_entry.table_id = 2001
        yield response

    call = MagicMock(spec=grpc.aio.UnaryStreamCall)
    call.__aiter__.side_effect = responses
    call.done.return_value = False
    patched_aio["stub"].Read.return_value = call
    iterator = client.list_table_entries()
    pending = asyncio.create_task(anext(iterator))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1.0)
        if ending == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            await pending
            await iterator.aclose()
        call.cancel.assert_called_once()
        assert client.is_connected
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
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


def _counter_p4info(counter_id: int = 5001) -> p4info_pb2.P4Info:
    p4info = p4info_pb2.P4Info()
    counter = p4info.counters.add()
    counter.preamble.id = counter_id
    counter.preamble.name = "MyIngress.pkts"
    return p4info


async def test_read_counter_single_index(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio, info_index=P4InfoIndex(_counter_p4info()))
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


@pytest.mark.parametrize(
    ("operation", "action", "empty_result"),
    [
        ("table-one", "VERIFY", []),
        ("table-one", "VERIFY_AND_COMMIT", []),
        ("table-all", "COMMIT", []),
        ("counter-cell", "VERIFY_AND_COMMIT", 0),
        ("counter-all", "VERIFY", {}),
        ("counter-all", "COMMIT", {}),
    ],
)
async def test_async_indexed_read_waits_for_pipeline_and_rechecks_p4info(
    patched_aio: dict[str, Any], operation: str, action: str, empty_result: object
) -> None:
    info = _make_p4info_index().raw
    info.counters.add().CopyFrom(_counter_p4info().counters[0])
    client = await _connect(patched_aio, info_index=P4InfoIndex(info))
    original = client.info_index
    candidate = p4info_pb2.P4Info()
    candidate.CopyFrom(info)
    candidate.tables[0].preamble.id = 3001
    candidate.counters[0].preamble.id = 6001
    stub = patched_aio["stub"]
    pushing, release_push = asyncio.Event(), asyncio.Event()
    refreshing, release_refresh = asyncio.Event(), asyncio.Event()

    async def hold_push(_request: Any) -> None:
        pushing.set()
        await release_push.wait()

    async def hold_refresh(_request: Any) -> Any:
        refreshing.set()
        await release_refresh.wait()
        response = p4runtime_pb2.GetForwardingPipelineConfigResponse()
        response.config.p4info.CopyFrom(candidate)
        return response

    async def empty() -> AsyncIterator[Any]:
        yield p4runtime_pb2.ReadResponse()

    async def read() -> Any:
        if operation.startswith("counter-"):
            return await client.read_counter(
                "MyIngress.pkts", 3 if operation == "counter-cell" else None
            )
        table = "MyIngress.ipv4_lpm" if operation == "table-one" else None
        return [entry async for entry in client.list_table_entries(table)]

    stub.SetForwardingPipelineConfig.side_effect = hold_push
    stub.GetForwardingPipelineConfig = AsyncMock(side_effect=hold_refresh)
    stub.Read.side_effect = lambda _request, timeout: empty()
    files = () if action == "COMMIT" else (str(candidate).encode(), b"{}")
    pipeline = asyncio.create_task(client.push_pipeline(*files, action=action))
    pending = None
    try:
        await asyncio.wait_for(pushing.wait(), timeout=1.0)
        pending = asyncio.create_task(read())
        done, _ = await asyncio.wait((pending,), timeout=0.02)
        assert not done
        stub.Read.assert_not_called()

        release_push.set()
        if action == "COMMIT":
            await asyncio.wait_for(refreshing.wait(), timeout=1.0)
            assert not pending.done()
            stub.Read.assert_not_called()
            release_refresh.set()
        await asyncio.wait_for(pipeline, timeout=1.0)
        if action == "VERIFY":
            assert await asyncio.wait_for(pending, timeout=1.0) == empty_result
            assert client.info_index is original
        else:
            with pytest.raises(P4RuntimeError, match="P4Info changed"):
                await asyncio.wait_for(pending, timeout=1.0)
            stub.Read.assert_not_called()
            assert await read() == empty_result
        stub.Read.assert_called_once()
    finally:
        release_push.set()
        release_refresh.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await asyncio.gather(pipeline, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("ending", ["response", "cancel", "deadline"])
async def test_async_pipeline_waits_for_inflight_table_and_counter_reads(
    patched_aio: dict[str, Any], ending: str
) -> None:
    info = _make_p4info_index().raw
    info.counters.add().CopyFrom(_counter_p4info().counters[0])
    client = await _connect(patched_aio, info_index=P4InfoIndex(info))
    stub = patched_aio["stub"]
    table_waiting, counter_waiting, release_table, release_counter, push_entered = (
        asyncio.Event() for _ in range(5)
    )

    async def hold_response(request: Any, *, timeout: float) -> AsyncIterator[Any]:
        response = p4runtime_pb2.ReadResponse()
        if request.entities[0].HasField("table_entry"):
            table_waiting.set()
            await release_table.wait()
            if ending == "deadline":
                raise grpc.aio.AioRpcError(
                    grpc.StatusCode.DEADLINE_EXCEEDED, (), (), "read deadline"
                )
            response.entities.add().table_entry.table_id = 2001
            yield response
        else:
            cell = response.entities.add().counter_entry
            cell.counter_id = 5001
            cell.index.index = 3
            cell.data.packet_count = 42
            yield response
            counter_waiting.set()
            await release_counter.wait()

    async def push() -> None:
        push_entered.set()
        await client.push_pipeline(b"", b"{}")

    stub.Read.side_effect = hold_response
    iterator = client.list_table_entries()
    table = asyncio.create_task(anext(iterator))
    counter = asyncio.create_task(client.read_counter("MyIngress.pkts", 3))
    pipeline = None
    try:
        await asyncio.wait_for(table_waiting.wait(), timeout=1.0)
        await asyncio.wait_for(counter_waiting.wait(), timeout=1.0)
        pipeline = asyncio.create_task(push())
        await asyncio.wait_for(push_entered.wait(), timeout=1.0)
        stub.SetForwardingPipelineConfig.assert_not_called()

        if ending == "cancel":
            table.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(table, timeout=1.0)
        elif ending == "deadline":
            release_table.set()
            with pytest.raises(P4RuntimeError, match="read deadline"):
                await asyncio.wait_for(table, timeout=1.0)
        else:
            release_table.set()
            entry = await asyncio.wait_for(table, timeout=1.0)
            assert entry["table"] == "MyIngress.ipv4_lpm"
        stub.SetForwardingPipelineConfig.assert_not_called()
        assert not pipeline.done()

        release_counter.set()
        assert await asyncio.wait_for(counter, timeout=1.0) == 42
        await asyncio.wait_for(pipeline, timeout=1.0)
        stub.SetForwardingPipelineConfig.assert_awaited_once()
        if ending == "response":
            with pytest.raises(P4RuntimeError, match="P4Info changed while reading"):
                await anext(iterator)
    finally:
        release_table.set()
        release_counter.set()
        await asyncio.gather(table, counter, return_exceptions=True)
        await iterator.aclose()
        if pipeline is not None:
            await asyncio.gather(pipeline, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("cell", [3, None], ids=["single-cell", "all-cells"])
@pytest.mark.parametrize("change", ["reconnect", "p4info"])
@pytest.mark.parametrize("pause", ["before-response", "after-response", "empty"])
async def test_read_counter_keeps_its_connection_and_p4info(
    patched_aio: dict[str, Any],
    mocker: MockerFixture,
    cell: int | None,
    change: str,
    pause: str,
) -> None:
    original_id, updated_id = 5001, 6001
    client = await _connect(patched_aio, info_index=P4InfoIndex(_counter_p4info(original_id)))
    original_stub = current_stub = patched_aio["stub"]
    waiting, release = asyncio.Event(), asyncio.Event()
    counter = "MyIngress.pkts"

    def response(counter_id: int, packets: int) -> Any:
        result = p4runtime_pb2.ReadResponse()
        indices = (3, 7) if cell is None else (cell,)
        for index in indices:
            entry = result.entities.add().counter_entry
            entry.counter_id = counter_id
            entry.index.index = index
            entry.data.packet_count = packets
        return result

    async def delayed() -> AsyncIterator[Any]:
        if pause == "after-response":
            yield response(original_id, 42)
        waiting.set()
        await release.wait()
        if pause == "before-response":
            yield response(original_id, 42)

    call = MagicMock(spec=grpc.aio.UnaryStreamCall)
    call.__aiter__.side_effect = delayed
    call.done.return_value = False
    original_stub.Read.return_value = call
    pending = asyncio.create_task(client.read_counter(counter, cell))

    try:
        await asyncio.wait_for(waiting.wait(), timeout=1.0)
        request = original_stub.Read.call_args.args[0]
        assert request.entities[0].counter_entry.counter_id == original_id
        active_id = original_id
        if change == "reconnect":
            current_index = client.info_index
            await client.disconnect()
            current_stub, stream = _replace_async_stream(mocker)
            current_stub.GetForwardingPipelineConfig = AsyncMock(
                return_value=_pipeline_reply(current_index)
            )
            await stream.push_arbitration()
            await client.connect()
        else:
            active_id = updated_id
            client._info_index = P4InfoIndex(_counter_p4info(active_id))
        release.set()

        error = ConnectionError if change == "reconnect" else P4RuntimeError
        message = "closed during a counter read" if change == "reconnect" else "P4Info changed"
        with pytest.raises(error, match=message):
            await asyncio.wait_for(pending, timeout=1.0)
        call.cancel.assert_called_once()
        if change == "reconnect":
            current_stub.Read.assert_not_called()

        async def fresh() -> AsyncIterator[Any]:
            yield response(active_id, 53)

        current_stub.Read.return_value = fresh()
        assert await client.read_counter(counter, cell) == (
            53 if cell is not None else {3: 53, 7: 53}
        )
        entry = current_stub.Read.call_args.args[0].entities[0].counter_entry
        assert entry.counter_id == active_id
        assert entry.HasField("index") == (cell is not None)
        if cell is not None:
            assert entry.index.index == cell

        async def empty() -> AsyncIterator[Any]:
            yield p4runtime_pb2.ReadResponse()

        current_stub.Read.return_value = empty()
        assert await client.read_counter(counter, cell) == (0 if cell is not None else {})
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await client.disconnect()


async def test_cancelling_counter_read_ends_unfinished_rpc(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio, info_index=P4InfoIndex(_counter_p4info()))
    waiting = asyncio.Event()

    async def responses() -> AsyncIterator[Any]:
        waiting.set()
        await asyncio.Future()
        yield p4runtime_pb2.ReadResponse()

    call = MagicMock(spec=grpc.aio.UnaryStreamCall)
    call.__aiter__.side_effect = responses
    call.done.return_value = False
    patched_aio["stub"].Read.return_value = call
    pending = asyncio.create_task(client.read_counter("MyIngress.pkts"))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1.0)
        pending.cancel()
        with pytest.raises(AsyncOperationCancelledError, match="read_counter cancelled"):
            await pending
        call.cancel.assert_called_once()
        assert client.is_connected
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("operation", ["table", "counter"])
@pytest.mark.parametrize(
    "ending",
    ["disconnect", "disconnect-status", "reconnect", "caller-and-disconnect", "active-status"],
)
async def test_async_reads_distinguish_disconnect_from_cancellation(
    patched_aio: dict[str, Any], mocker: MockerFixture, operation: str, ending: str
) -> None:
    info = _make_p4info_index().raw
    info.counters.add().CopyFrom(_counter_p4info().counters[0])
    client = await _connect(patched_aio, info_index=P4InfoIndex(info))
    waiting, release = asyncio.Event(), asyncio.Event()
    status_cancelled = grpc.aio.AioRpcError(grpc.StatusCode.CANCELLED, (), (), "RPC cancelled")
    transport_cancelled = asyncio.CancelledError("channel closed")
    rpc_error = (
        status_cancelled
        if ending in ("active-status", "disconnect-status")
        else transport_cancelled
    )
    original_stub = patched_aio["stub"]

    async def read_response() -> Any:
        waiting.set()
        await release.wait()
        raise rpc_error

    async def responses() -> AsyncIterator[Any]:
        yield await read_response()

    async def read() -> Any:
        if operation == "table":
            return [entry async for entry in client.list_table_entries()]
        return await client.read_counter("MyIngress.pkts", 3)

    original_stub.Read.side_effect = lambda request, timeout: responses()
    pending = asyncio.create_task(read())
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1.0)
        if ending != "active-status":
            if ending == "caller-and-disconnect":

                async def close() -> None:
                    pending.cancel("caller cancelled")
                    release.set()

                patched_aio["channel"].close.side_effect = close
            await client.disconnect()
        if ending == "reconnect":
            replacement, stream = _replace_async_stream(mocker)
            replacement.GetForwardingPipelineConfig = AsyncMock(
                return_value=_pipeline_reply(P4InfoIndex(info))
            )
            await stream.push_arbitration()
            await client.connect()
            replacement.Read.assert_not_called()
        release.set()

        if ending == "caller-and-disconnect":
            error = asyncio.CancelledError if operation == "table" else AsyncOperationCancelledError
            with pytest.raises(error) as raised:
                await pending
            cause = raised.value if operation == "table" else raised.value.__cause__
            assert isinstance(cause, asyncio.CancelledError)
        elif ending == "active-status":
            with pytest.raises(AsyncOperationCancelledError, match="RPC cancelled") as raised:
                await pending
            assert raised.value.__cause__ is status_cancelled
            assert client.is_connected
        else:
            message = (
                "closed while reading table entries"
                if operation == "table"
                else "closed during a counter read"
            )
            with pytest.raises(ConnectionError, match=message) as raised:
                await pending
            assert raised.value.__cause__ is rpc_error
        original_stub.Read.assert_called_once()
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
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


async def test_packet_in_buffer_keeps_pending_packets_and_still_calls_handlers(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    stream = patched_aio["stream"]
    packets = [index.to_bytes(2, "big") for index in range(258)]
    received: list[bytes] = []
    last_packet = asyncio.Event()

    async def handler(payload: bytes, meta: dict[str, int]) -> None:
        received.append(payload)
        if payload == packets[-1]:
            last_packet.set()

    try:
        await client.on_packet_in(handler)
        for packet in packets:
            await stream.push_packet(packet)
        await asyncio.wait_for(last_packet.wait(), timeout=5.0)

        assert received == packets
        queue = client._packet_in_queue
        assert queue is not None and queue.qsize() == 256
        buffered = [await client.expect_packet_in() for _ in range(256)]
        assert [payload for payload, _ in buffered] == packets[:256]

        await stream.push_packet(b"next")
        payload, _ = await client.expect_packet_in(timeout=1.0)
        assert payload == b"next"
    finally:
        await client.disconnect()


async def test_expect_packet_in_timeout(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio)
    try:
        with pytest.raises(P4RuntimeError, match="no PacketIn within"):
            await client.expect_packet_in(timeout=0.2)
    finally:
        await client.disconnect()


@pytest.mark.parametrize("ending", ["disconnect", "eof", "rpc-error"])
async def test_connection_close_wakes_packet_in_waiters(
    patched_aio: dict[str, Any], ending: str
) -> None:
    client = await _connect(patched_aio)
    stream_task = client._stream_task
    assert stream_task is not None
    waiters = [asyncio.create_task(client.expect_packet_in(timeout=30.0)) for _ in range(2)]
    error = grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, (), (), "server stopped")
    try:
        await asyncio.sleep(0)
        if ending == "disconnect":
            await client.disconnect()
        else:
            await patched_aio["stream"].finish(error if ending == "rpc-error" else None)
            await asyncio.wait_for(asyncio.shield(stream_task), timeout=2.0)

        for waiter in waiters:
            with pytest.raises(ConnectionError, match="closed") as result:
                await asyncio.wait_for(asyncio.shield(waiter), timeout=0.5)
            if ending == "rpc-error":
                assert result.value.__cause__ is error

        retry_stream = _FakeStream()
        patched_aio["stub"].StreamChannel.side_effect = lambda _requests: retry_stream
        await retry_stream.push_arbitration()
        await client.connect()
        await retry_stream.push_packet(b"new connection")
        assert (await client.expect_packet_in(timeout=1.0))[0] == b"new connection"
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        await client.disconnect()


async def test_cancelled_packet_in_wait_leaves_next_packet_available(
    patched_aio: dict[str, Any],
) -> None:
    client = await _connect(patched_aio)
    waiter = asyncio.create_task(client.expect_packet_in())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(AsyncOperationCancelledError):
            await waiter
        await patched_aio["stream"].push_packet(b"next")
        assert (await client.expect_packet_in(timeout=1.0))[0] == b"next"
    finally:
        await client.disconnect()


async def test_packet_in_arrival_racing_with_cancellation(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio)
    waiter = asyncio.create_task(client.expect_packet_in())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        queue = client._packet_in_queue
        assert queue is not None
        packet = (b"arriving", {})
        queue.put_nowait(packet)
        asyncio.get_running_loop().call_soon(waiter.cancel)

        try:
            received = await waiter
        except AsyncOperationCancelledError:
            received = await client.expect_packet_in(timeout=0.5)
        assert received == packet
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


@pytest.mark.parametrize("change", ["reconnect", "p4info"])
@pytest.mark.parametrize(
    ("method", "args", "stdout", "expected"),
    [
        ("write_register", (4, 17), b"", None),
        ("read_register", (4,), b"RuntimeCmd: MyIngress.test_register[4]= 53\n", 53),
        (
            "read_register",
            (),
            b"RuntimeCmd: MyIngress.test_register= " + b", ".join([b"53"] * 16) + b"\n",
            [53] * 16,
        ),
    ],
    ids=["write", "read-cell", "read-all"],
)
async def test_async_register_commands_keep_their_connection_and_p4info(
    patched_aio: dict[str, Any],
    mocker: MockerFixture,
    change: str,
    method: str,
    args: tuple[int, ...],
    stdout: bytes,
    expected: int | list[int] | None,
) -> None:
    name = "MyIngress.test_register"
    client = await _connect(patched_aio)
    operation = getattr(client, method)
    started = asyncio.Event()
    lookup = client.info_index.register_by_name

    def note_preparation(register: str) -> Any:
        started.set()
        return lookup(register)

    mocker.patch.object(client.info_index, "register_by_name", side_effect=note_preparation)
    mocker.patch("p4net.control.async_client.shutil.which", return_value="simple_switch_CLI")
    proc = MagicMock(returncode=0, communicate=AsyncMock(return_value=(stdout, b"")))
    spawn = mocker.patch("asyncio.create_subprocess_exec", return_value=proc)
    pending = None
    reconnecting = None
    try:
        async with client._pipeline_lock:
            pending = asyncio.create_task(operation(name, *args))
            await asyncio.wait_for(started.wait(), timeout=1.0)
            if change == "reconnect":
                current_index = client.info_index
                await client.disconnect()
                stub, replacement = _replace_async_stream(mocker)
                stub.GetForwardingPipelineConfig = AsyncMock(
                    return_value=_pipeline_reply(current_index)
                )
                await replacement.push_arbitration()
                channel = MagicMock(close=AsyncMock())
                mocker.patch(
                    "p4net.control.async_client.grpc.aio.insecure_channel", return_value=channel
                )
                reconnecting = asyncio.create_task(client.connect())
            else:
                client._info_index = _make_p4info_index()

        error = ConnectionError if change == "reconnect" else P4RuntimeError
        message = "closed during a register" if change == "reconnect" else "P4Info changed"
        with pytest.raises(error, match=message):
            await asyncio.wait_for(pending, timeout=1.0)
        if reconnecting is not None:
            await asyncio.wait_for(reconnecting, timeout=1.0)
        spawn.assert_not_awaited()

        assert await operation(name, *args) == expected
        spawn.assert_awaited_once()
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if reconnecting is not None:
            await asyncio.gather(reconnecting, return_exceptions=True)
        await client.disconnect()


@pytest.mark.parametrize("interrupt", ["launch", "command", "timeout"])
async def test_async_register_holds_lifecycle_until_thrift_child_exits(
    patched_aio: dict[str, Any], mocker: MockerFixture, interrupt: str
) -> None:
    client = await _connect(patched_aio)
    launched, entered, killed, finish = (asyncio.Event() for _ in range(4))
    starting = asyncio.Event()
    input_bytes = []

    async def communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
        input_bytes.append(input)
        entered.set()
        await killed.wait()
        await finish.wait()
        return b"", b""

    proc = MagicMock(returncode=0, communicate=AsyncMock(side_effect=communicate), wait=AsyncMock())
    proc.kill.side_effect = killed.set

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        starting.set()
        await launched.wait()
        return proc

    mocker.patch("p4net.control.async_client.shutil.which", return_value="simple_switch_CLI")
    mocker.patch("asyncio.create_subprocess_exec", side_effect=spawn)
    if interrupt != "launch":
        launched.set()
    command = asyncio.create_task(
        client.write_register(
            "MyIngress.test_register", 4, 17, timeout=0.05 if interrupt == "timeout" else 5.0
        )
    )
    disconnecting = None
    try:
        await asyncio.wait_for(starting.wait(), timeout=1.0)
        if interrupt == "launch":
            command.cancel()
            await asyncio.sleep(0)
            assert not command.done()
            launched.set()
        elif interrupt == "command":
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            command.cancel()

        await asyncio.wait_for(killed.wait(), timeout=1.0)
        disconnecting = asyncio.create_task(client.disconnect())
        await asyncio.sleep(0)
        assert client.is_connected and not disconnecting.done()
        if interrupt != "timeout":
            command.cancel()
            await asyncio.sleep(0)
            assert not command.done()
        finish.set()

        if interrupt == "timeout":
            with pytest.raises(P4RuntimeError, match="timed out") as raised:
                await asyncio.wait_for(command, timeout=1.0)
            assert type(raised.value) is P4RuntimeError
        else:
            with pytest.raises(AsyncOperationCancelledError, match="cancelled"):
                await asyncio.wait_for(command, timeout=1.0)
        assert input_bytes == [
            b"" if interrupt == "launch" else b"register_write MyIngress.test_register 4 17\n"
        ]
        proc.kill.assert_called_once()
        await asyncio.wait_for(disconnecting, timeout=1.0)
    finally:
        launched.set()
        finish.set()
        await asyncio.gather(command, return_exceptions=True)
        if disconnecting is not None:
            await asyncio.gather(disconnecting, return_exceptions=True)
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
