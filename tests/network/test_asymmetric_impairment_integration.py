"""End-to-end asymmetric link impairment test.

Run with:

    sudo -E env "PATH=$PATH" pytest \\
      --run-integration --run-p4c --run-bmv2 \\
      -m "integration and requires_p4c and requires_bmv2" \\
      tests/network/test_asymmetric_impairment_integration.py
"""

from __future__ import annotations

import re
import socket
import uuid
from pathlib import Path

import pytest

from p4net import Network
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
    cache = Path("/tmp") / f"asym-it-cache-{uuid.uuid4().hex[:8]}"
    compiler = P4Compiler(cache_dir=cache)
    result = compiler.compile(_TWO_PORT_SWAP)
    return {"bmv2_json": result.bmv2_json, "p4info": result.p4info}


_PING_AVG_RE = re.compile(r"min/avg/max(?:/mdev)? = [\d.]+/([\d.]+)/")


def _parse_ping_avg_ms(stdout: bytes) -> float:
    text = stdout.decode("utf-8", errors="replace")
    m = _PING_AVG_RE.search(text)
    if m is None:
        raise AssertionError(f"could not parse ping rtt summary from output:\n{text}")
    return float(m.group(1))


def test_asymmetric_delay_round_trip(compiled: dict[str, Path], tmp_path: Path) -> None:
    """h1→s1 delay = 200 ms; s1→h2 delay = 20 ms; ping RTT ≈ 220 ms."""
    suffix = _suffix()
    h1, h2, s1 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    grpc, thrift = _two_free_ports()
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    topo.add_switch(s1, p4_src=_TWO_PORT_SWAP, grpc_port=grpc, thrift_port=thrift)
    # h1 ↔ s1: delay h1 → s1 = 200ms.
    topo.add_link(h1, s1, port_b=1, delay_a_to_b="200ms")
    # h2 ↔ s1: delay s1 → h2 = 20ms (a=h2, b=s1, b→a direction).
    topo.add_link(h2, s1, port_b=2, delay_b_to_a="20ms")
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        h1r, h2r = net.host(h1), net.host(h2)
        # Static ARP both directions.
        h1r.exec(
            [
                "ip",
                "neigh",
                "replace",
                "10.0.0.2",
                "lladdr",
                "00:00:00:00:00:02",
                "dev",
                f"{h1}-eth0",
                "nud",
                "permanent",
            ]
        )
        h2r.exec(
            [
                "ip",
                "neigh",
                "replace",
                "10.0.0.1",
                "lladdr",
                "00:00:00:00:00:01",
                "dev",
                f"{h2}-eth0",
                "nud",
                "permanent",
            ]
        )
        # Run 5 pings with a 3-second per-reply deadline; expect avg RTT ~220 ms.
        result = h1r.exec(
            ["ping", "-4", "-c", "5", "-W", "3", "-w", "20", "10.0.0.2"],
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"ping failed (rc={result.returncode}): "
            f"stderr={result.stderr.decode(errors='replace')!r}, "
            f"stdout={result.stdout.decode(errors='replace')!r}"
        )
        avg_ms = _parse_ping_avg_ms(result.stdout)
        # Allow for kernel scheduling jitter under suite load; netem can drift
        # well above the nominal value when other integration tests are running
        # concurrent BMv2 processes.
        assert 200.0 < avg_ms < 320.0, (
            f"asymmetric delay not in expected range: avg={avg_ms} ms "
            f"(expected ~220 ms ± tolerance)\nfull output:\n"
            f"{result.stdout.decode(errors='replace')}"
        )
        # Stash for the report.
        result.stdout  # noqa: B018  (kept for debugging)
    finally:
        net.stop()


def test_symmetric_base_plus_a_to_b_extra(compiled: dict[str, Path], tmp_path: Path) -> None:
    """100ms symmetric base + 100ms a_to_b extra on h1↔s1; RTT ≈ 300 ms.

    h1 → s1: delay 100ms (base) + 100ms (extra) = 200ms one way.
    s1 → h1: delay 100ms (base only) = 100ms one way.
    h2 ↔ s1: unimpaired.
    Round trip h1 → h2 → h1 ≈ 200 + 100 = 300 ms.
    """
    suffix = _suffix()
    h1, h2, s1 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    grpc, thrift = _two_free_ports()
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    topo.add_switch(s1, p4_src=_TWO_PORT_SWAP, grpc_port=grpc, thrift_port=thrift)
    topo.add_link(
        h1,
        s1,
        port_b=1,
        delay="100ms",
        delay_a_to_b_extra="100ms",
    )
    topo.add_link(h2, s1, port_b=2)
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        h1r, h2r = net.host(h1), net.host(h2)
        h1r.exec(
            [
                "ip",
                "neigh",
                "replace",
                "10.0.0.2",
                "lladdr",
                "00:00:00:00:00:02",
                "dev",
                f"{h1}-eth0",
                "nud",
                "permanent",
            ]
        )
        h2r.exec(
            [
                "ip",
                "neigh",
                "replace",
                "10.0.0.1",
                "lladdr",
                "00:00:00:00:00:01",
                "dev",
                f"{h2}-eth0",
                "nud",
                "permanent",
            ]
        )
        result = h1r.exec(
            ["ping", "-4", "-c", "5", "-W", "3", "-w", "20", "10.0.0.2"],
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"ping failed (rc={result.returncode}): "
            f"stderr={result.stderr.decode(errors='replace')!r}"
        )
        avg_ms = _parse_ping_avg_ms(result.stdout)
        assert 280.0 < avg_ms < 360.0, (
            f"base+extra delay not in expected range: avg={avg_ms} ms "
            f"(expected ~300 ms ± tolerance)"
        )
    finally:
        net.stop()
