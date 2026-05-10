"""End-to-end IPv6 tests against a running BMv2 + P4Runtime stack.

Run with:

    sudo -E env "PATH=$PATH" pytest \\
      --run-integration --run-p4c --run-bmv2 \\
      -m "integration and requires_p4c and requires_bmv2" \\
      tests/network/test_ipv6_integration.py
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from p4net import Network
from p4net.cli import CommandDispatcher
from p4net.compiler import P4Compiler
from p4net.topo import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_p4c,
    pytest.mark.requires_bmv2,
]

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "p4"
_TWO_PORT_SWAP = _FIXTURES / "two_port_swap.p4"


def _suffix() -> str:
    return uuid.uuid4().hex[:6]


def _two_free_ports() -> tuple[int, int]:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as a,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as b,
    ):
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        return int(a.getsockname()[1]), int(b.getsockname()[1])


@pytest.fixture(scope="session")
def compiled() -> dict[str, Path]:
    cache = Path("/tmp") / f"ipv6-it-cache-{uuid.uuid4().hex[:8]}"
    compiler = P4Compiler(cache_dir=cache)
    result = compiler.compile(_TWO_PORT_SWAP)
    return {"bmv2_json": result.bmv2_json, "p4info": result.p4info}


def _seed_arp(host: object, target_ip: str, target_mac: str, iface: str) -> None:
    host.exec(  # type: ignore[attr-defined]
        [
            "ip",
            "neigh",
            "replace",
            target_ip,
            "lladdr",
            target_mac,
            "dev",
            iface,
            "nud",
            "permanent",
        ]
    )


def _seed_nd(host: object, target_ip6: str, target_mac: str, iface: str) -> None:
    host.exec(  # type: ignore[attr-defined]
        [
            "ip",
            "-6",
            "neigh",
            "replace",
            target_ip6,
            "lladdr",
            target_mac,
            "dev",
            iface,
            "nud",
            "permanent",
        ]
    )


# ---------------------------------------------------------------------------
# 1. IPv4-only host disables IPv6
# ---------------------------------------------------------------------------


def test_ipv4_only_host_disables_ipv6(compiled: dict[str, Path], tmp_path: Path) -> None:
    suffix = _suffix()
    h1, s1 = f"h{suffix}", f"s{suffix}"
    grpc, thrift = _two_free_ports()
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    topo.add_switch(s1, p4_src=_TWO_PORT_SWAP, grpc_port=grpc, thrift_port=thrift)
    topo.add_link(h1, s1, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        host = net.host(h1)
        iface = f"{h1}-eth0"
        out = host.exec(
            ["sysctl", "-n", f"net.ipv6.conf.{iface}.disable_ipv6"], capture_output=True
        )
        assert out.stdout.decode().strip() == "1"
        # No IPv6 addresses configured (link-local is suppressed too).
        out = host.exec(["ip", "-6", "addr", "show", "dev", iface], capture_output=True)
        # An iface with disable_ipv6=1 has no IPv6 lines; allow either no
        # output or no global / link-local addresses.
        assert b"inet6" not in out.stdout
    finally:
        net.stop()


# ---------------------------------------------------------------------------
# 2. IPv6-only host enables IPv6 with autoconf=0, accept_ra=0
# ---------------------------------------------------------------------------


def test_ipv6_only_host_enabled_with_autoconf_zero(
    compiled: dict[str, Path], tmp_path: Path
) -> None:
    suffix = _suffix()
    h1, s1 = f"h{suffix}", f"s{suffix}"
    grpc, thrift = _two_free_ports()
    topo = Topology()
    topo.add_host(h1, ip6="fd00::1/64", mac="00:00:00:00:00:01")
    topo.add_switch(s1, p4_src=_TWO_PORT_SWAP, grpc_port=grpc, thrift_port=thrift)
    topo.add_link(h1, s1, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        host = net.host(h1)
        iface = f"{h1}-eth0"
        for key in ("disable_ipv6", "accept_ra", "autoconf"):
            out = host.exec(["sysctl", "-n", f"net.ipv6.conf.{iface}.{key}"], capture_output=True)
            assert out.stdout.decode().strip() == "0", key
        out = host.exec(["ip", "-6", "addr", "show", "dev", iface], capture_output=True)
        assert b"fd00::1/64" in out.stdout
    finally:
        net.stop()


# ---------------------------------------------------------------------------
# 3. Dual-stack ping (IPv4 and IPv6 both work)
# ---------------------------------------------------------------------------


def test_dual_stack_ping(compiled: dict[str, Path], tmp_path: Path) -> None:
    suffix = _suffix()
    h1, h2, s1 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    grpc, thrift = _two_free_ports()
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", ip6="fd00::1/64", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip="10.0.0.2/24", ip6="fd00::2/64", mac="00:00:00:00:00:02")
    topo.add_switch(s1, p4_src=_TWO_PORT_SWAP, grpc_port=grpc, thrift_port=thrift)
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(h2, s1, port_b=2)
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        h1r, h2r = net.host(h1), net.host(h2)
        _seed_arp(h1r, "10.0.0.2", "00:00:00:00:00:02", f"{h1}-eth0")
        _seed_arp(h2r, "10.0.0.1", "00:00:00:00:00:01", f"{h2}-eth0")
        _seed_nd(h1r, "fd00::2", "00:00:00:00:00:02", f"{h1}-eth0")
        _seed_nd(h2r, "fd00::1", "00:00:00:00:00:01", f"{h2}-eth0")
        assert h1r.ping("10.0.0.2", count=2, timeout=2.0) is True
        assert h1r.ping("fd00::2", count=2, timeout=2.0) is True
    finally:
        net.stop()


# ---------------------------------------------------------------------------
# 4. CLI ping6
# ---------------------------------------------------------------------------


def test_cli_ping6(compiled: dict[str, Path], tmp_path: Path) -> None:
    suffix = _suffix()
    h1, h2, s1 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    grpc, thrift = _two_free_ports()
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", ip6="fd00::1/64", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip="10.0.0.2/24", ip6="fd00::2/64", mac="00:00:00:00:00:02")
    topo.add_switch(s1, p4_src=_TWO_PORT_SWAP, grpc_port=grpc, thrift_port=thrift)
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(h2, s1, port_b=2)
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        h1r, h2r = net.host(h1), net.host(h2)
        _seed_nd(h1r, "fd00::2", "00:00:00:00:00:02", f"{h1}-eth0")
        _seed_nd(h2r, "fd00::1", "00:00:00:00:00:01", f"{h2}-eth0")
        d = CommandDispatcher(net, color=False)
        out = d.dispatch(f"{h1} ping6 fd00::2 2 2.0")
        assert out == "OK"
    finally:
        net.stop()


# ---------------------------------------------------------------------------
# 5. IPv6 default route
# ---------------------------------------------------------------------------


def test_ipv6_default_route_present(compiled: dict[str, Path], tmp_path: Path) -> None:
    suffix = _suffix()
    h1, s1 = f"h{suffix}", f"s{suffix}"
    grpc, thrift = _two_free_ports()
    topo = Topology()
    topo.add_host(
        h1,
        ip6="fd00:1::2/64",
        default_route6="fd00:1::1",
        mac="00:00:00:00:00:01",
    )
    topo.add_switch(s1, p4_src=_TWO_PORT_SWAP, grpc_port=grpc, thrift_port=thrift)
    topo.add_link(h1, s1, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        host = net.host(h1)
        out = host.exec(["ip", "-6", "route", "show", "default"], capture_output=True)
        assert b"fd00:1::1" in out.stdout
    finally:
        net.stop()


# Keep the imports alive for explicit test discovery.
_KEEP: Iterator[None] | None = None
