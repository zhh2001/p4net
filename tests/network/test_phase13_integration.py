"""Phase-13 end-to-end tests: IPv6 LPM, pingall6, and human-form table dump.

Run with:

    sudo -E env "PATH=$PATH" pytest \\
      --run-integration --run-p4c --run-bmv2 \\
      -m "integration and requires_p4c and requires_bmv2" \\
      tests/network/test_phase13_integration.py
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

_EXAMPLE_IPV6 = (
    Path(__file__).resolve().parent.parent.parent / "examples" / "ipv6_lpm" / "ipv6_lpm.p4"
)
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
def compiled_ipv6() -> dict[str, Path]:
    cache = Path("/tmp") / f"phase13-cache-{uuid.uuid4().hex[:8]}"
    compiler = P4Compiler(cache_dir=cache)
    result = compiler.compile(_EXAMPLE_IPV6)
    return {"bmv2_json": result.bmv2_json, "p4info": result.p4info}


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


# Shared bring-up for the IPv6 LPM cases.
@pytest.fixture
def ipv6_network(compiled_ipv6: dict[str, Path], tmp_path: Path) -> Iterator[Network]:
    suffix = _suffix()
    h1, h2, s1 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    grpc, thrift = _two_free_ports()
    topo = Topology()
    topo.add_host(h1, ip6="fd00::1/64", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip6="fd00::2/64", mac="00:00:00:00:00:02")
    topo.add_switch(s1, p4_src=_EXAMPLE_IPV6, grpc_port=grpc, thrift_port=thrift)
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(h2, s1, port_b=2)
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        h1r, h2r = net.host(h1), net.host(h2)
        _seed_nd(h1r, "fd00::2", "00:00:00:00:00:02", f"{h1}-eth0")
        _seed_nd(h2r, "fd00::1", "00:00:00:00:00:01", f"{h2}-eth0")
        sw = net.switch(s1)
        sw.client.insert_table_entry(
            "MyIngress.ipv6_lpm",
            {"hdr.ipv6.dstAddr": "fd00::1/128"},
            "MyIngress.set_egress_port",
            {"port": 1},
        )
        sw.client.insert_table_entry(
            "MyIngress.ipv6_lpm",
            {"hdr.ipv6.dstAddr": "fd00::2/128"},
            "MyIngress.set_egress_port",
            {"port": 2},
        )
        net._test_h1 = h1  # type: ignore[attr-defined]
        net._test_h2 = h2  # type: ignore[attr-defined]
        net._test_s1 = s1  # type: ignore[attr-defined]
        yield net
    finally:
        net.stop()


def _names(network: Network) -> tuple[str, str, str]:
    return (
        network._test_h1,  # type: ignore[attr-defined]
        network._test_h2,  # type: ignore[attr-defined]
        network._test_s1,  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# 1. IPv6 LPM end-to-end with counter
# ---------------------------------------------------------------------------


def test_ipv6_lpm_ping_and_counter(ipv6_network: Network) -> None:
    h1, _h2, s1 = _names(ipv6_network)
    h1r = ipv6_network.host(h1)
    assert h1r.ping("fd00::2", count=2, timeout=2.0) is True
    sw = ipv6_network.switch(s1)
    cell = sw.client.read_counter("MyIngress.ipv6_pkts", index=2)
    assert cell.packet_count >= 1


# ---------------------------------------------------------------------------
# 2. <switch> table dump renders IPv6 in human form (visible payoff)
# ---------------------------------------------------------------------------


_PAYOFF_OUTPUT_FILE = Path("/tmp/p4net-phase13-payoff.txt")


def test_ipv6_table_dump_human_form(ipv6_network: Network) -> None:
    _h1, _h2, s1 = _names(ipv6_network)
    d = CommandDispatcher(ipv6_network, color=False)
    out = d.dispatch(f"{s1} table dump MyIngress.ipv6_lpm")
    # Stash the captured output for the report.
    _PAYOFF_OUTPUT_FILE.write_text(out)
    assert "fd00::1/128" in out
    assert "fd00::2/128" in out
    # Raw byte representation must NOT be in the output.
    assert "b'\\xfd" not in out
    # Action params decoded as decimals.
    assert "'port': '1'" in out
    assert "'port': '2'" in out


# ---------------------------------------------------------------------------
# 3. pingall6 over a dual-stack topology
# ---------------------------------------------------------------------------


def test_pingall6_dual_stack(tmp_path: Path) -> None:
    suffix = _suffix()
    h1, h2, h3, h4, s1 = (
        f"h{suffix}a",
        f"h{suffix}b",
        f"h{suffix}c",
        f"h{suffix}d",
        f"s{suffix}",
    )
    grpc, thrift = _two_free_ports()
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", ip6="fd00::1/64", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip="10.0.0.2/24", ip6="fd00::2/64", mac="00:00:00:00:00:02")
    topo.add_host(h3, ip="10.0.0.3/24", ip6="fd00::3/64", mac="00:00:00:00:00:03")
    # h4 is v4-only — should NOT appear in pingall6's eligible set.
    topo.add_host(h4, ip="10.0.1.1/24", mac="00:00:00:00:00:04")
    topo.add_switch(s1, p4_src=_TWO_PORT_SWAP, grpc_port=grpc, thrift_port=thrift)
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(h2, s1, port_b=2)
    topo.add_link(h3, s1, port_b=3)
    topo.add_link(h4, s1, port_b=4)
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        # two_port_swap pipeline only swaps ports 1↔2; with the third host
        # we verify v6 connectivity via ND broadcast (which the swap handles
        # via egress port 1↔2 only). To isolate pingall6's eligible filter
        # rather than data-plane reachability, restrict the test to h1↔h2
        # explicitly:
        h1r, h2r = net.host(h1), net.host(h2)
        _seed_nd(h1r, "fd00::2", "00:00:00:00:00:02", f"{h1}-eth0")
        _seed_nd(h2r, "fd00::1", "00:00:00:00:00:01", f"{h2}-eth0")
        result = net.pingall6(count=1, timeout=2.0)
        keys = set(result.keys())
        # h4 (v4-only) excluded; only h1/h2/h3 ordered pairs appear.
        v6_hosts = {h1, h2, h3}
        for src in v6_hosts:
            for dst in v6_hosts:
                if src != dst:
                    assert (src, dst) in keys
        assert not any(h4 in (src, dst) for src, dst in keys)
        # h1↔h2 reach each other (the swap takes care of those ports).
        assert result[(h1, h2)] is True
        assert result[(h2, h1)] is True
        # And pingall (v4) DOES include h4.
        v4 = net.pingall(count=1, timeout=2.0)
        v4_keys = set(v4.keys())
        assert any(h4 in (src, dst) for src, dst in v4_keys)
    finally:
        net.stop()


# ---------------------------------------------------------------------------
# 4. CLI pingall6 rendering
# ---------------------------------------------------------------------------


def test_cli_pingall6_rendering(ipv6_network: Network) -> None:
    h1, h2, _s1 = _names(ipv6_network)
    d = CommandDispatcher(ipv6_network, color=False)
    out = d.dispatch("pingall6")
    assert h1 in out
    assert h2 in out
    # The matrix's success summary appears as 'N/M succeeded'.
    assert "/" in out and "succeeded" in out


# Used in the report.
__all__ = ["_PAYOFF_OUTPUT_FILE"]
