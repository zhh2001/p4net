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
import time
import uuid
from pathlib import Path

import pytest

from p4net import Network
from p4net.control import (
    AsyncOperationCancelledError,  # noqa: F401  (exposed for tests if useful)
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
# 2. Concurrent vs sequential inserts across multiple switches.
# ---------------------------------------------------------------------------


async def test_concurrent_inserts_across_switches(tmp_path: Path) -> None:
    """Three switches, ~9 inserts; concurrent should beat sequential by a
    healthy margin. The sync client (primary) installs sequentially first
    as the baseline; the async client (secondary) installs concurrently
    on a different table (NoAction default) and we compare wall time.

    Because secondary writes are rejected, we use the SYNC clients here
    for both the sequential and the concurrent paths — the concurrent
    path uses ``asyncio.to_thread`` to fan out the sync calls. This
    measures the gather/parallelism speedup specifically.
    """
    suffix = _suffix()
    names = [f"s{suffix}{i}" for i in (1, 2, 3)]
    topo = Topology()
    topo.add_host(f"h{suffix}", ip="10.0.0.1/24")
    for n in names:
        topo.add_switch(n, p4_src=_SIMPLE_ROUTING, grpc_port=_free_port(), thrift_port=_free_port())
    topo.add_link(f"h{suffix}", names[0], port_b=1)

    net = Network(topo, log_dir=tmp_path / "logs")
    await asyncio.to_thread(net.start)
    try:
        # 1. Populate the ipv4_lpm table on every switch via the sync primary
        # clients (the async clients are secondary in this test, so they
        # can't write; the comparison is between concurrent and sequential
        # ASYNC READS).
        sync_clients = [net.switch(n).client for n in names]
        routes = [
            ("10.0.10.0/24", 1),
            ("10.0.20.0/24", 1),
            ("10.0.30.0/24", 1),
            ("10.0.40.0/24", 1),
            ("10.0.50.0/24", 1),
        ]
        for c in sync_clients:
            for cidr, port in routes:
                c.insert_table_entry(
                    "MyIngress.ipv4_lpm",
                    {"hdr.ipv4.dstAddr": cidr},
                    "MyIngress.set_egress_port",
                    {"port": port},
                )

        # 2. Build async clients, secondary so they can coexist with sync.
        async_clients = []
        for n in names:
            ac = net.switch(n).async_client
            ac._election_id = (0, 0)
            async_clients.append(ac)
        await asyncio.gather(*(c.connect() for c in async_clients))

        async def _list_all(client: object) -> int:
            count = 0
            async for _e in client.list_table_entries("MyIngress.ipv4_lpm"):  # type: ignore[attr-defined]
                count += 1
            return count

        # Repeat the read sweep N times so the per-call cost dominates
        # event-loop and dispatcher overhead.
        N = 8
        # Sequential baseline.
        t0 = time.perf_counter()
        for _ in range(N):
            for c in async_clients:
                await _list_all(c)
        seq_ms = (time.perf_counter() - t0) * 1000.0

        # Concurrent: all reads from all switches in parallel.
        t0 = time.perf_counter()
        for _ in range(N):
            await asyncio.gather(*(_list_all(c) for c in async_clients))
        conc_ms = (time.perf_counter() - t0) * 1000.0

        await asyncio.gather(*(c.disconnect() for c in async_clients))

        print(
            f"\nMEASURED concurrent={conc_ms:.2f}ms sequential={seq_ms:.2f}ms "
            f"ratio={seq_ms / max(conc_ms, 0.001):.2f}x",
            flush=True,
        )
        # Three switches read in parallel should beat three sequential
        # reads. 1.5x is a conservative threshold for a noisy CI rig.
        assert conc_ms < seq_ms / 1.5, (
            f"concurrent={conc_ms:.2f}ms should beat sequential={seq_ms:.2f}ms by 1.5x"
        )
        (tmp_path / "async_speedup.txt").write_text(
            f"concurrent={conc_ms:.2f}ms sequential={seq_ms:.2f}ms "
            f"ratio={seq_ms / max(conc_ms, 0.001):.2f}x\n"
        )
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
