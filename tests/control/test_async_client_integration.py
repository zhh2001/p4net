"""End-to-end tests for ``AsyncP4RuntimeClient`` against a real BMv2 stack.

Marked with ``integration + requires_p4c + requires_bmv2``. Each test is
``async def`` and runs under pytest-asyncio's ``auto`` mode.

Run with:

    sudo -E env "PATH=$PATH" pytest \\
      --run-integration --run-p4c --run-bmv2 \\
      -m "integration and requires_p4c and requires_bmv2" \\
      tests/control/test_async_client_integration.py
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from pathlib import Path
from typing import Any

import grpc
import pytest
from google.rpc import code_pb2
from p4.v1 import p4runtime_pb2
from pytest_mock import MockerFixture

from p4net import Network
from p4net.control import (
    AsyncOperationCancelledError,  # noqa: F401  (exposed for tests if useful)
    AsyncP4RuntimeClient,
    ConnectionError,
    DuplicateEntryError,
    EntryNotFoundError,
    NoSuchRegisterError,
    NotPrimaryError,
)
from p4net.runtime import BMv2Switch
from p4net.topo import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_p4c,
    pytest.mark.requires_bmv2,
]

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "p4"
_SIMPLE_ROUTING = _FIXTURES / "simple_routing.p4"
_REGISTER_DEMO = _FIXTURES / "register_demo.p4"


def _suffix() -> str:
    return uuid.uuid4().hex[:6]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# 1. Basic lifecycle on a single switch.
# ---------------------------------------------------------------------------


async def test_basic_lifecycle(tmp_path: Path) -> None:
    suffix = _suffix()
    s = f"s{suffix}"
    h = f"h{suffix}"
    topo = Topology()
    topo.add_host(h, ip="10.0.0.1/24")
    topo.add_switch(s, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port())
    topo.add_link(h, s, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    try:
        sw = net.switch(s)
        client = sw.async_client
        # Async client takes secondary (sync client owns primary at ts=startup ms).
        # Use election_id=(0,0) explicitly so we don't trip the not-primary check.
        client._election_id = (0, 0)
        await client.connect()
        assert client.is_connected
        assert not client.is_primary
        # Reads work; writes are rejected for secondary clients (see test below).
        seen: list[dict] = []
        async for e in client.list_table_entries("MyIngress.ipv4_lpm"):
            seen.append(e)
        # No entries yet — the sync client hasn't programmed anything.
        assert seen == []
        await client.disconnect()
        assert not client.is_connected
    finally:
        await asyncio.to_thread(net.stop)


@pytest.mark.parametrize("mode", ["async", "worker", "owner"])
async def test_network_stop_closes_async_connection(
    tmp_path: Path, mocker: MockerFixture, mode: str
) -> None:
    suffix = _suffix()
    switch = f"s{suffix}"
    host = f"h{suffix}"
    topo = Topology()
    topo.add_host(host, ip="10.0.0.1/24")
    topo.add_switch(
        switch, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port()
    )
    topo.add_link(host, switch, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    sw = net.switch(switch)
    client = sw.async_client
    client._election_id = (0, 0)
    try:
        await client.connect()
        owner = asyncio.get_running_loop()
        channel = client._channel
        stream_task = client._stream_task
        close = channel.close
        closed_on: list[asyncio.AbstractEventLoop] = []

        async def close_on_owner() -> None:
            closed_on.append(asyncio.get_running_loop())
            await close()

        mocker.patch.object(channel, "close", side_effect=close_on_owner)
        if mode == "worker":
            await asyncio.to_thread(net.stop)
        elif mode == "owner":
            net.stop()
            await net.astop()
        else:
            await net.astop()
        assert closed_on == [owner]
        assert channel.get_state() == grpc.ChannelConnectivity.SHUTDOWN
        assert stream_task is not None and stream_task.done()
        assert client._channel is None
        assert client._stream_task is None
        assert client._loop is None
        assert not client.is_connected
        assert not client.is_primary
        assert sw._async_client is None
        assert not sw.bmv2.is_running()
        assert not net.is_running
        await net.astop()
    finally:
        await client.disconnect()
        await asyncio.to_thread(net.stop)


async def test_network_astop_during_async_connect(tmp_path: Path, mocker: MockerFixture) -> None:
    suffix = _suffix()
    switch = f"s{suffix}"
    host = f"h{suffix}"
    topo = Topology()
    topo.add_host(host, ip="10.0.0.1/24")
    topo.add_switch(
        switch, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port()
    )
    topo.add_link(host, switch, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    client = net.switch(switch).async_client
    started = asyncio.Event()

    async def hold_arbitration() -> None:
        started.set()
        await asyncio.Event().wait()

    mocker.patch.object(client, "_stream_consumer", side_effect=hold_arbitration)
    connecting = asyncio.create_task(client.connect(timeout=30.0))
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        channel = client._channel
        stream_task = client._stream_task
        await net.astop()
        with pytest.raises(ConnectionError, match="closed"):
            await asyncio.wait_for(asyncio.shield(connecting), timeout=0.5)
        assert channel.get_state() == grpc.ChannelConnectivity.SHUTDOWN
        assert stream_task is not None and stream_task.done()
        assert client._channel is None
        assert not client.is_connected
        assert not net.is_running
    finally:
        connecting.cancel()
        await asyncio.gather(connecting, return_exceptions=True)
        await client.disconnect()
        await asyncio.to_thread(net.stop)


async def test_stream_end_clears_state_and_allows_reconnect(tmp_path: Path) -> None:
    suffix = _suffix()
    switch = f"s{suffix}"
    host = f"h{suffix}"
    topo = Topology()
    topo.add_host(host, ip="10.0.0.1/24")
    topo.add_switch(
        switch, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port()
    )
    topo.add_link(host, switch, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    sw = net.switch(switch)
    client = sw.async_client
    client._election_id = (sw.client.election_id[0] + 1, 0)
    replacement = BMv2Switch(
        f"{switch}-replacement",
        device_id=client.device_id,
        grpc_port=client.grpc_address[1],
        thrift_port=sw.bmv2.thrift_port,
        bmv2_json=sw.compile_result.bmv2_json,
        port_to_iface={},
        log_dir=tmp_path / "restart-logs",
    )
    try:
        await client.connect()
        assert client.is_connected
        assert client.is_primary
        channel = client._channel
        stream_task = client._stream_task
        assert stream_task is not None
        await asyncio.to_thread(sw.bmv2.stop)
        await asyncio.wait_for(asyncio.shield(stream_task), timeout=5.0)
        assert not client.is_connected
        assert not client.is_primary
        assert channel.get_state() == grpc.ChannelConnectivity.SHUTDOWN
        assert client._channel is None
        assert client._stream_task is None
        with pytest.raises(ConnectionError):
            client.list_table_entries("MyIngress.ipv4_lpm")

        await asyncio.to_thread(replacement.start)
        await asyncio.to_thread(replacement.wait_until_ready)
        await client.connect()
        assert client.is_connected
        assert client.is_primary
        assert client._channel is not channel
        await client.push_pipeline(
            sw.compile_result.p4info.read_bytes(), sw.compile_result.bmv2_json.read_bytes()
        )
        await client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.1.0/24"},
            "MyIngress.set_egress_port",
            {"port": 2},
        )
        assert [entry["params"] async for entry in client.list_table_entries()] == [
            {"port": b"\x02"}
        ]
    finally:
        try:
            await client.disconnect()
        finally:
            try:
                await asyncio.to_thread(replacement.stop)
            finally:
                await asyncio.to_thread(net.stop)


@pytest.mark.parametrize("cancel_mode", ["task", "wait-for"])
async def test_cancelled_connect_releases_resources_and_can_retry(
    tmp_path: Path, mocker: MockerFixture, cancel_mode: str
) -> None:
    suffix = _suffix()
    switch = f"s{suffix}"
    host = f"h{suffix}"
    topo = Topology()
    topo.add_host(host, ip="10.0.0.1/24")
    topo.add_switch(
        switch, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port()
    )
    topo.add_link(host, switch, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    client = net.switch(switch).async_client
    client._election_id = (0, 0)
    consumer_started = asyncio.Event()
    allow_arbitration = asyncio.Event()
    real_consumer = client._stream_consumer

    async def hold_arbitration() -> None:
        consumer_started.set()
        await allow_arbitration.wait()
        await real_consumer()

    mocker.patch.object(client, "_stream_consumer", new=hold_arbitration)
    attempt = asyncio.create_task(client.connect())
    try:
        await asyncio.wait_for(consumer_started.wait(), timeout=5.0)
        channel = client._channel
        stream = client._stream_call
        stream_task = client._stream_task
        assert stream_task is not None
        if cancel_mode == "wait-for":
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(attempt, timeout=0)
        else:
            attempt.cancel()
            with pytest.raises(asyncio.CancelledError):
                await attempt
        assert not client.is_connected
        assert channel.get_state() == grpc.ChannelConnectivity.SHUTDOWN
        assert stream.cancelled()
        assert stream_task.done()
        assert client._channel is None
        assert client._stream_task is None

        allow_arbitration.set()
        await client.connect()
        assert client.is_connected
        assert not client.is_primary
        assert client._channel is not channel
        assert [entry async for entry in client.list_table_entries("MyIngress.ipv4_lpm")] == []
    finally:
        attempt.cancel()
        await asyncio.gather(attempt, return_exceptions=True)
        try:
            await client.disconnect()
        finally:
            await asyncio.to_thread(net.stop)


# ---------------------------------------------------------------------------
# 2. Concurrent reads across multiple switches.
# ---------------------------------------------------------------------------


async def test_concurrent_reads_across_switches(tmp_path: Path) -> None:
    """Read each switch's routes concurrently and compare their full contents."""
    suffix = _suffix()
    names = [f"s{suffix}{i}" for i in (1, 2, 3)]
    topo = Topology()
    topo.add_host(f"h{suffix}", ip="10.0.0.1/24")
    for n in names:
        topo.add_switch(n, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port())
    topo.add_link(f"h{suffix}", names[0], port_b=1)

    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    async_clients = []
    try:
        expected = []
        for switch_number, name in enumerate(names, start=1):
            entries = []
            for subnet in (10, 20, 30, 40, 50):
                net.switch(name).client.insert_table_entry(
                    "MyIngress.ipv4_lpm",
                    {"hdr.ipv4.dstAddr": f"10.{switch_number}.{subnet}.0/24"},
                    "MyIngress.set_egress_port",
                    {"port": switch_number},
                )
                entries.append(
                    {
                        "table": "MyIngress.ipv4_lpm",
                        "match": {"hdr.ipv4.dstAddr": (bytes([10, switch_number, subnet, 0]), 24)},
                        "action": "MyIngress.set_egress_port",
                        "params": {"port": bytes([switch_number])},
                        "priority": None,
                    }
                )
            expected.append(entries)

        for n in names:
            ac = net.switch(n).async_client
            ac._election_id = (0, 0)
            async_clients.append(ac)
        await asyncio.gather(*(c.connect() for c in async_clients))

        async def _list_all(client: AsyncP4RuntimeClient) -> list[dict[str, Any]]:
            entries = [entry async for entry in client.list_table_entries("MyIngress.ipv4_lpm")]
            return sorted(entries, key=lambda entry: entry["match"]["hdr.ipv4.dstAddr"])

        sequential = [await _list_all(client) for client in async_clients]
        assert sequential == expected
        concurrent = await asyncio.gather(*(_list_all(client) for client in async_clients))
        assert concurrent == expected
    finally:
        try:
            await asyncio.gather(*(client.disconnect() for client in async_clients))
        finally:
            await asyncio.to_thread(net.stop)


# ---------------------------------------------------------------------------
# 3. Register operations via the async Thrift wrapper.
# ---------------------------------------------------------------------------


async def test_register_async(tmp_path: Path) -> None:
    suffix = _suffix()
    s = f"s{suffix}"
    h = f"h{suffix}"
    topo = Topology()
    topo.add_host(h, ip="10.0.0.1/24")
    topo.add_switch(s, p4_src=_REGISTER_DEMO, grpc_port=_free_port(), thrift_port=_free_port())
    topo.add_link(h, s, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    try:
        sw = net.switch(s)
        client = sw.async_client
        client._election_id = (0, 0)
        await client.connect()
        try:
            await client.write_register("MyIngress.test_register", index=5, value=0xCAFE)
            value = await client.read_register("MyIngress.test_register", index=5)
            assert value == 0xCAFE
            full = await client.read_register("MyIngress.test_register")
            assert isinstance(full, list)
            assert full[5] == 0xCAFE
            assert all(v == 0 for i, v in enumerate(full) if i != 5)
            # Unknown register name → NoSuchRegisterError.
            with pytest.raises(NoSuchRegisterError):
                await client.read_register("missing_register")
        finally:
            await client.disconnect()
    finally:
        await asyncio.to_thread(net.stop)


# ---------------------------------------------------------------------------
# 4. Disconnect during streaming.
# ---------------------------------------------------------------------------


async def test_disconnect_during_streaming(tmp_path: Path) -> None:
    suffix = _suffix()
    s = f"s{suffix}"
    h = f"h{suffix}"
    topo = Topology()
    topo.add_host(h, ip="10.0.0.1/24")
    topo.add_switch(s, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port())
    topo.add_link(h, s, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    try:
        sw = net.switch(s)
        client = sw.async_client
        client._election_id = (0, 0)
        await client.connect()
        # Start a list iterator but don't consume — then disconnect.
        ait = client.list_table_entries("MyIngress.ipv4_lpm")
        # Don't fully exhaust; disconnect should be clean even with an open iter.
        await client.disconnect()
        assert not client.is_connected
        # The iterator should be safe to drop without dangling tasks.
        del ait
    finally:
        await asyncio.to_thread(net.stop)


# ---------------------------------------------------------------------------
# 5. Mastership: secondary client cannot write.
# ---------------------------------------------------------------------------


async def test_mastership_secondary_rejects_writes(tmp_path: Path) -> None:
    suffix = _suffix()
    s = f"s{suffix}"
    h = f"h{suffix}"
    topo = Topology()
    topo.add_host(h, ip="10.0.0.1/24")
    topo.add_switch(s, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port())
    topo.add_link(h, s, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    try:
        sw = net.switch(s)
        client = sw.async_client
        # Force secondary status: election_id (0,0) is always lower than the
        # sync client's millisecond-time-since-epoch.
        client._election_id = (0, 0)
        await client.connect()
        try:
            assert not client.is_primary
            # Write should be rejected; BMv2 returns PERMISSION_DENIED for
            # non-primary writes, which we translate to NotPrimaryError.
            with pytest.raises(NotPrimaryError):
                await client.insert_table_entry(
                    "MyIngress.ipv4_lpm",
                    {"hdr.ipv4.dstAddr": "10.0.0.0/24"},
                    "MyIngress.set_egress_port",
                    {"port": 2},
                )
        finally:
            await client.disconnect()
    finally:
        await asyncio.to_thread(net.stop)


async def test_push_pipeline_without_primary_preserves_index_and_can_retry(tmp_path: Path) -> None:
    suffix = _suffix()
    switch = f"s{suffix}"
    host = f"h{suffix}"
    topo = Topology()
    topo.add_host(host, ip="10.0.0.1/24")
    topo.add_switch(
        switch, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port()
    )
    topo.add_link(host, switch, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    sw = net.switch(switch)
    client = sw.async_client
    client._election_id = (0, 0)
    info = sw.compile_result.p4info.read_bytes()
    config = sw.compile_result.bmv2_json.read_bytes()
    try:
        await client.connect()
        index = client.info_index
        channel = client._channel
        stream = client._stream_call
        with pytest.raises(NotPrimaryError) as raised:
            await client.push_pipeline(info, config)
        assert isinstance(raised.value.__cause__, grpc.aio.AioRpcError)
        assert raised.value.__cause__.code() == grpc.StatusCode.PERMISSION_DENIED
        assert client.is_connected
        assert client.info_index is index
        assert [entry async for entry in client.list_table_entries()] == []

        client._election_id = (sw.client.election_id[0] + 1, 0)
        request = p4runtime_pb2.StreamMessageRequest()
        request.arbitration.device_id = client.device_id
        request.arbitration.election_id.high = client.election_id[0]
        assert client._arbitration_event is not None
        client._arbitration_event.clear()
        assert client._outgoing is not None
        await client._outgoing.put(request)
        await asyncio.wait_for(client._arbitration_event.wait(), timeout=5.0)
        assert client.is_primary
        await client.push_pipeline(info, config)
        assert client.info_index is not index
        assert client._channel is channel
        assert client._stream_call is stream
        await client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.1.0/24"},
            "MyIngress.set_egress_port",
            {"port": 2},
        )
        assert [entry["params"] async for entry in client.list_table_entries()] == [
            {"port": b"\x02"}
        ]
    finally:
        await net.astop()


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("duplicate", DuplicateEntryError),
        ("modify-missing", EntryNotFoundError),
        ("delete-missing", EntryNotFoundError),
    ],
)
async def test_table_write_decodes_batch_errors_and_remains_usable(
    tmp_path: Path, operation: str, expected: type[Exception]
) -> None:
    suffix = _suffix()
    switch, host = f"s{suffix}", f"h{suffix}"
    topo = Topology()
    topo.add_host(host, ip="10.0.0.1/24")
    topo.add_switch(
        switch, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port()
    )
    topo.add_link(host, switch, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    sw = net.switch(switch)
    client = sw.async_client
    client._election_id = (sw.client.election_id[0] + 1, 0)
    table, action = "MyIngress.ipv4_lpm", "MyIngress.set_egress_port"
    match = {"hdr.ipv4.dstAddr": "10.0.1.0/24"}
    missing = {"hdr.ipv4.dstAddr": "10.0.2.0/24"}
    try:
        await client.connect()
        index, channel, stream = client.info_index, client._channel, client._stream_call
        await client.insert_table_entry(table, match, action, {"port": 1})
        with pytest.raises(expected) as raised:
            if operation == "duplicate":
                await client.insert_table_entry(table, match, action, {"port": 1})
            elif operation == "modify-missing":
                await client.modify_table_entry(table, missing, action, {"port": 2})
            else:
                await client.delete_table_entry(table, missing)
        assert isinstance(raised.value.__cause__, grpc.aio.AioRpcError)
        assert raised.value.__cause__.code() == grpc.StatusCode.UNKNOWN
        assert client.is_connected and client.is_primary
        assert client.info_index is index
        assert client._channel is channel and client._stream_call is stream
        assert [entry["params"] async for entry in client.list_table_entries()] == [
            {"port": b"\x01"}
        ]
        await client.modify_table_entry(table, match, action, {"port": 2})
        assert [entry["params"] async for entry in client.list_table_entries()] == [
            {"port": b"\x02"}
        ]
        await client.delete_table_entry(table, match)
        assert [entry async for entry in client.list_table_entries()] == []
    finally:
        await net.astop()


async def test_primary_state_tracks_competing_clients(tmp_path: Path) -> None:
    suffix = _suffix()
    switch = f"s{suffix}"
    host = f"h{suffix}"
    topo = Topology()
    topo.add_host(host, ip="10.0.0.1/24")
    topo.add_switch(
        switch, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port()
    )
    topo.add_link(host, switch, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    sw = net.switch(switch)
    first = sw.async_client
    election_high = sw.client.election_id[0]
    first._election_id = (election_high + 1, 0)
    second = AsyncP4RuntimeClient(
        first.grpc_address,
        device_id=first.device_id,
        info_index=first.info_index,
        election_id=(election_high + 2, 0),
    )
    table = "MyIngress.ipv4_lpm"
    match = {"hdr.ipv4.dstAddr": "10.0.1.0/24"}
    action = "MyIngress.set_egress_port"
    try:
        await first.connect()
        assert first.is_primary
        await first.insert_table_entry(table, match, action, {"port": 1})
        assert first._arbitration_event is not None
        first._arbitration_event.clear()

        await second.connect()
        await asyncio.wait_for(first._arbitration_event.wait(), timeout=5.0)
        assert first._arbitration.status.code == code_pb2.ALREADY_EXISTS
        assert not first.is_primary
        assert first.is_connected
        assert second.is_primary
        with pytest.raises(NotPrimaryError):
            await first.modify_table_entry(table, match, action, {"port": 2})
        await second.modify_table_entry(table, match, action, {"port": 2})
        assert [entry["params"] async for entry in first.list_table_entries(table)] == [
            {"port": b"\x02"}
        ]

        # Renew arbitration on the existing stream with a higher election ID.
        original_stream = first._stream_call
        first._election_id = (election_high + 3, 0)
        request = p4runtime_pb2.StreamMessageRequest()
        request.arbitration.device_id = first.device_id
        request.arbitration.election_id.high = first.election_id[0]
        request.arbitration.election_id.low = first.election_id[1]
        first._arbitration_event.clear()
        assert second._arbitration_event is not None
        second._arbitration_event.clear()
        assert first._outgoing is not None
        await first._outgoing.put(request)
        await asyncio.wait_for(first._arbitration_event.wait(), timeout=5.0)
        await asyncio.wait_for(second._arbitration_event.wait(), timeout=5.0)
        assert first._arbitration.status.code == code_pb2.OK
        assert first.is_primary
        assert first.is_connected
        assert first._stream_call is original_stream
        assert second._arbitration.status.code == code_pb2.ALREADY_EXISTS
        assert not second.is_primary
        assert second.is_connected
        with pytest.raises(NotPrimaryError):
            await second.delete_table_entry(table, match)
        await first.delete_table_entry(table, match)
        assert [entry async for entry in first.list_table_entries(table)] == []
    finally:
        try:
            await asyncio.gather(first.disconnect(), second.disconnect())
        finally:
            await asyncio.to_thread(net.stop)
