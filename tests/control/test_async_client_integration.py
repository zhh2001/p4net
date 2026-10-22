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

import pytest

from p4net import Network
from p4net.control import (
    AsyncOperationCancelledError,  # noqa: F401  (exposed for tests if useful)
    AsyncP4RuntimeClient,
    NoSuchRegisterError,
    NotPrimaryError,
)
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
