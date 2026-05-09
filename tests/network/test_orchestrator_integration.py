"""End-to-end integration tests for `p4net.network.Network`.

Every test stacks three markers — `integration` (requires root), `requires_p4c`
(needs the `p4c` binary), and `requires_bmv2` (needs `simple_switch_grpc`) —
so the suite is fully gated by the conftest. Run with:

    sudo -E env "PATH=$PATH" pytest \\
      --run-integration --run-p4c --run-bmv2 \\
      -m "integration and requires_p4c and requires_bmv2"

Each test uses random suffixes on every node / interface name so a partial
failure of an earlier run does not collide with this one.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from p4net import Network
from p4net.network import RunningHost
from p4net.topo import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_p4c,
    pytest.mark.requires_bmv2,
]

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "p4"
_SIMPLE_ROUTING = _FIXTURES / "simple_routing.p4"
_TWO_PORT_SWAP = _FIXTURES / "two_port_swap.p4"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _suffix() -> str:
    return uuid.uuid4().hex[:6]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _list_netns() -> list[str]:
    """Return the names of namespaces currently present on the host."""
    result = subprocess.run(
        ["ip", "-o", "netns", "list"],
        capture_output=True,
        check=False,
        text=True,
    )
    names: list[str] = []
    for line in result.stdout.splitlines():
        # Each line: "name (id: 0)" or "name".
        names.append(line.split()[0])
    return names


def _list_iface_names_root() -> list[str]:
    """Return interface names visible in the root namespace."""
    result = subprocess.run(
        ["ip", "-o", "link", "show"],
        capture_output=True,
        check=False,
        text=True,
    )
    names: list[str] = []
    for line in result.stdout.splitlines():
        # "1: lo: <LOOPBACK,UP,LOWER_UP> ..."
        parts = line.split(":", 2)
        if len(parts) >= 2:
            names.append(parts[1].strip().split("@")[0])
    return names


def _add_static_arp(host: RunningHost, target_ip: str, target_mac: str) -> None:
    iface = next(iter(host.interfaces))
    host.exec(
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


# ---------------------------------------------------------------------------
# 1. Single host
# ---------------------------------------------------------------------------


def test_single_host_loopback(tmp_path: Path) -> None:
    """A single-host topology brings up a namespace with `lo` up; pinging
    127.0.0.1 (loopback's canonical IP) confirms the namespace was created
    and `lo` was brought up.
    """
    suffix = _suffix()
    h1 = f"h{suffix}"
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24")
    ns_before = set(_list_netns())
    with Network(topo, log_dir=tmp_path / "logs") as net:
        host = net.host(h1)
        assert host.ping("127.0.0.1") is True
    # After context manager exits, the namespace should be gone.
    assert h1 not in _list_netns()
    assert set(_list_netns()) - ns_before == set()


# ---------------------------------------------------------------------------
# 2. Two hosts via one switch with table programming
# ---------------------------------------------------------------------------


def test_two_hosts_one_switch_with_table_programming(tmp_path: Path) -> None:
    suffix = _suffix()
    h1, h2, s1 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    topo.add_switch(
        s1,
        p4_src=_SIMPLE_ROUTING,
        grpc_port=_free_port(),
        thrift_port=_free_port(),
    )
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(h2, s1, port_b=2)
    with Network(topo, log_dir=tmp_path / "logs", pcap_dir=tmp_path / "pcaps") as net:
        h_a = net.host(h1)
        h_b = net.host(h2)
        # Static ARP both directions.
        _add_static_arp(h_a, "10.0.0.2", "00:00:00:00:00:02")
        _add_static_arp(h_b, "10.0.0.1", "00:00:00:00:00:01")
        # Push two table entries (one per direction).
        sw = net.switch(s1)
        sw.client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.0.2/32"},
            "MyIngress.set_egress_port",
            {"port": 2},
        )
        sw.client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.0.1/32"},
            "MyIngress.set_egress_port",
            {"port": 1},
        )
        # Ping should now succeed.
        assert net.ping(h1, h2, count=2, timeout=2.0) is True
        # The egress-port-2 cell of the counter should be non-zero.
        from p4net.control import CounterData

        cell = sw.client.read_counter("MyIngress.ingress_pkts", 2)
        assert isinstance(cell, CounterData)
        assert cell.packet_count >= 1


# ---------------------------------------------------------------------------
# 3. Link impairment kills connectivity
# ---------------------------------------------------------------------------


def test_link_impairment_drops_all_traffic(tmp_path: Path) -> None:
    """Build the same topology as test 2 but with 100% loss on h1's link.
    Ping must fail. We build with the impairment from the start (rebuild
    semantics) rather than mutating a running Network.
    """
    suffix = _suffix()
    h1, h2, s1 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    topo.add_switch(
        s1,
        p4_src=_SIMPLE_ROUTING,
        grpc_port=_free_port(),
        thrift_port=_free_port(),
    )
    topo.add_link(h1, s1, port_b=1, loss_pct=100.0)
    topo.add_link(h2, s1, port_b=2)
    with Network(topo, log_dir=tmp_path / "logs") as net:
        _add_static_arp(net.host(h1), "10.0.0.2", "00:00:00:00:00:02")
        _add_static_arp(net.host(h2), "10.0.0.1", "00:00:00:00:00:01")
        sw = net.switch(s1)
        sw.client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.0.2/32"},
            "MyIngress.set_egress_port",
            {"port": 2},
        )
        sw.client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.0.1/32"},
            "MyIngress.set_egress_port",
            {"port": 1},
        )
        assert net.ping(h1, h2, count=2, timeout=2.0) is False


# ---------------------------------------------------------------------------
# 4. Crash recovery: external SIGKILL does not break stop()
# ---------------------------------------------------------------------------


def test_stop_after_external_bmv2_kill(tmp_path: Path) -> None:
    suffix = _suffix()
    h1, s1 = f"h{suffix}", f"s{suffix}"
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24")
    topo.add_switch(
        s1,
        p4_src=_SIMPLE_ROUTING,
        grpc_port=_free_port(),
        thrift_port=_free_port(),
    )
    topo.add_link(h1, s1, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        pid = net.switch(s1).bmv2.pid
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        # Give the kernel a moment to reap.
        time.sleep(0.5)
    finally:
        # stop() must not raise even though BMv2 is already gone.
        net.stop()
    assert h1 not in _list_netns()


# ---------------------------------------------------------------------------
# 5. Multi-switch chain (no table programming needed)
# ---------------------------------------------------------------------------


def test_multi_switch_chain_with_two_port_swap(tmp_path: Path) -> None:
    suffix = _suffix()
    h1, h2, s1, s2 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}a", f"s{suffix}b"
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    topo.add_switch(
        s1,
        p4_src=_TWO_PORT_SWAP,
        grpc_port=_free_port(),
        thrift_port=_free_port(),
    )
    topo.add_switch(
        s2,
        p4_src=_TWO_PORT_SWAP,
        grpc_port=_free_port(),
        thrift_port=_free_port(),
    )
    # h1<->s1.port1, s1.port2<->s2.port1, s2.port2<->h2
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(s1, s2, port_a=2, port_b=1)
    topo.add_link(s2, h2, port_a=2)
    with Network(topo, log_dir=tmp_path / "logs") as net:
        _add_static_arp(net.host(h1), "10.0.0.2", "00:00:00:00:00:02")
        _add_static_arp(net.host(h2), "10.0.0.1", "00:00:00:00:00:01")
        # Two sequential pings to give counters/flows time to settle.
        assert net.ping(h1, h2, count=3, timeout=3.0) is True


# ---------------------------------------------------------------------------
# 6. pingall across three hosts
# ---------------------------------------------------------------------------


def test_pingall_three_hosts(tmp_path: Path) -> None:
    suffix = _suffix()
    names = [f"h{suffix}{i}" for i in ("a", "b", "c")]
    macs = ["00:00:00:00:00:0a", "00:00:00:00:00:0b", "00:00:00:00:00:0c"]
    ips = ["10.0.0.1/24", "10.0.0.2/24", "10.0.0.3/24"]
    s1 = f"s{suffix}"
    topo = Topology()
    for name, ip, mac in zip(names, ips, macs, strict=True):
        topo.add_host(name, ip=ip, mac=mac)
    topo.add_switch(
        s1,
        p4_src=_SIMPLE_ROUTING,
        grpc_port=_free_port(),
        thrift_port=_free_port(),
    )
    for i, name in enumerate(names, start=1):
        topo.add_link(name, s1, port_b=i)
    with Network(topo, log_dir=tmp_path / "logs") as net:
        # Static ARP for every (src, dst) pair.
        host_objs = [net.host(n) for n in names]
        for src in host_objs:
            for dst, dst_mac in zip(host_objs, macs, strict=True):
                if src is dst:
                    continue
                _add_static_arp(src, dst.primary_ip, dst_mac)  # type: ignore[arg-type]
        # Push table entries: for each host, install a /32 route to its port.
        sw = net.switch(s1)
        for i, name in enumerate(names, start=1):
            host = net.host(name)
            sw.client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": f"{host.primary_ip}/32"},
                "MyIngress.set_egress_port",
                {"port": i},
            )
        result = net.pingall(count=2, timeout=3.0)
        # Every ordered pair of distinct hosts.
        expected_keys = {(a, b) for a in names for b in names if a != b}
        assert set(result.keys()) == expected_keys
        assert all(result.values()), f"pingall had failures: {result}"


# ---------------------------------------------------------------------------
# 7. Context-manager body raises; namespaces and veths still get cleaned up
# ---------------------------------------------------------------------------


def test_context_manager_cleans_up_on_exception(tmp_path: Path) -> None:
    suffix = _suffix()
    h1, h2, s1 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    topo.add_switch(
        s1,
        p4_src=_SIMPLE_ROUTING,
        grpc_port=_free_port(),
        thrift_port=_free_port(),
    )
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(h2, s1, port_b=2)
    iface_a = f"{h1}-eth0"
    iface_b = f"{h2}-eth0"
    sw_iface_1 = f"{s1}-eth1"
    sw_iface_2 = f"{s1}-eth2"
    netns_before = set(_list_netns())
    iface_before = set(_list_iface_names_root())
    with (
        pytest.raises(ValueError, match="boom"),
        Network(topo, log_dir=tmp_path / "logs"),
    ):
        raise ValueError("boom")
    netns_after = set(_list_netns())
    iface_after = set(_list_iface_names_root())
    assert h1 not in netns_after
    assert h2 not in netns_after
    assert netns_after - netns_before == set()
    # No leaked switch-side veth interfaces in root.
    assert sw_iface_1 not in iface_after
    assert sw_iface_2 not in iface_after
    # Host-side ifaces would have been in their own ns; if those ns's are
    # gone (asserted above) the kernel auto-removes them.
    assert iface_a not in iface_after
    assert iface_b not in iface_after
    assert iface_after - iface_before == set()
