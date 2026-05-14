"""Unit tests for ``p4net.control.AsyncP4RuntimeClient``. All gRPC mocked."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2
from pytest_mock import MockerFixture

import p4net.control  # noqa: F401  (sets protobuf python-impl env var)
from p4net.control import (
    AsyncOperationCancelledError,
    AsyncP4RuntimeClient,
    ConnectionError,
    EncodingError,
    NoSuchRegisterError,
    P4InfoIndex,
    P4RuntimeError,
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

    def cancel(self) -> None:
        self._cancelled = True
        self._responses.put_nowait(StopAsyncIteration)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        item = await self._responses.get()
        if item is StopAsyncIteration or self._cancelled:
            raise StopAsyncIteration
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


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_connect_then_disconnect_clean(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio)
    assert client.is_connected
    assert client.is_primary
    await client.disconnect()
    assert not client.is_connected


async def test_connect_election_id_auto(patched_aio: dict[str, Any]) -> None:
    client = await _connect(patched_aio)
    high, low = client.election_id
    assert high > 0  # ms since epoch is well above zero
    assert low == 0
    await client.disconnect()


async def test_explicit_secondary_stays_connected(patched_aio: dict[str, Any]) -> None:
    # status_code != 0 with election_id=(0,0) is an explicit secondary — allowed.
    client = await _connect(patched_aio, election_id=(0, 0), status_code=6)
    assert client.is_connected
    assert not client.is_primary
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
# Table CRUD
# ---------------------------------------------------------------------------


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
