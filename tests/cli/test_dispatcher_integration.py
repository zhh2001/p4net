"""End-to-end tests: CLI dispatcher driving a real BMv2 + P4Runtime stack.

Every case stacks `integration` (root needed for namespaces) plus
`requires_p4c` (compiles `simple_routing.p4`) and `requires_bmv2`
(launches `simple_switch_grpc`). Run with:

    sudo -E env "PATH=$PATH" pytest \\
      --run-integration --run-p4c --run-bmv2 \\
      -m "integration and requires_p4c and requires_bmv2" \\
      tests/cli/test_dispatcher_integration.py
"""

from __future__ import annotations

import socket
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from p4net import Network
from p4net.cli import CommandDispatcher
from p4net.cli.exceptions import CLIUsageError
from p4net.compiler import P4Compiler
from p4net.network import RunningHost
from p4net.topo import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_p4c,
    pytest.mark.requires_bmv2,
]

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "p4"
_SIMPLE_ROUTING = _FIXTURES / "simple_routing.p4"
_MATCH_TYPES = _FIXTURES / "match_types.p4"


def _suffix() -> str:
    return uuid.uuid4().hex[:6]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


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


@pytest.fixture(scope="session")
def compiled() -> dict[str, Path]:
    cache = Path("/tmp") / f"cli-it-cache-{uuid.uuid4().hex[:8]}"
    compiler = P4Compiler(cache_dir=cache)
    result = compiler.compile(_SIMPLE_ROUTING)
    return {"bmv2_json": result.bmv2_json, "p4info": result.p4info}


@pytest.fixture
def network(compiled: dict[str, Path], tmp_path: Path) -> Iterator[Network]:
    """Two hosts on a single switch; ARPs and table entries pre-seeded."""
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
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        # Pre-seed static ARP so ICMP unicast doesn't have to resolve at run time.
        _add_static_arp(net.host(h1), "10.0.0.2", "00:00:00:00:00:02")
        _add_static_arp(net.host(h2), "10.0.0.1", "00:00:00:00:00:01")
        # Pre-seed forwarding entries so test 2's pingall is meaningful.
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
        # Stash the resolved names so each test can refer to them.
        net._test_h1_name = h1  # type: ignore[attr-defined]
        net._test_h2_name = h2  # type: ignore[attr-defined]
        net._test_s1_name = s1  # type: ignore[attr-defined]
        yield net
    finally:
        net.stop()


def _names(net: Network) -> tuple[str, str, str]:
    return (
        net._test_h1_name,  # type: ignore[attr-defined]
        net._test_h2_name,  # type: ignore[attr-defined]
        net._test_s1_name,  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# 1. hosts and switches
# ---------------------------------------------------------------------------


def test_hosts_and_switches_listed(network: Network) -> None:
    h1, h2, s1 = _names(network)
    d = CommandDispatcher(network, color=False)
    hosts_out = d.dispatch("hosts")
    assert h1 in hosts_out
    assert h2 in hosts_out
    assert "10.0.0.1" in hosts_out
    assert "10.0.0.2" in hosts_out
    sw_out = d.dispatch("switches")
    assert s1 in sw_out
    # gRPC address starts with 127.0.0.1 (random port)
    assert "127.0.0.1:" in sw_out


# ---------------------------------------------------------------------------
# 2. pingall after ARP + table entries returns all-True
# ---------------------------------------------------------------------------


def test_pingall_succeeds(network: Network) -> None:
    d = CommandDispatcher(network, color=False)
    out = d.dispatch("pingall 2 2.0")
    h1, h2, _s1 = _names(network)
    assert "succeeded" in out
    assert "2/2 succeeded" in out
    assert h1 in out
    assert h2 in out


# ---------------------------------------------------------------------------
# 3. <switch> table list
# ---------------------------------------------------------------------------


def test_switch_table_list(network: Network) -> None:
    _h1, _h2, s1 = _names(network)
    d = CommandDispatcher(network, color=False)
    out = d.dispatch(f"{s1} table list")
    assert "MyIngress.ipv4_lpm" in out
    assert "lpm" in out
    assert "hdr.ipv4.dstAddr" in out


# ---------------------------------------------------------------------------
# 4. table add then dump shows the entry
# ---------------------------------------------------------------------------


def test_switch_table_add_then_dump(network: Network) -> None:
    _h1, _h2, s1 = _names(network)
    d = CommandDispatcher(network, color=False)
    # The fixture pre-loaded /32 entries for 10.0.0.1 and 10.0.0.2; add a
    # different /32 so this test's add+dump round-trips a fresh entry.
    out_add = d.dispatch(
        f"{s1} table add MyIngress.ipv4_lpm "
        "match: hdr.ipv4.dstAddr=10.0.0.5/32 "
        "action: MyIngress.set_egress_port "
        "params: port=2"
    )
    assert out_add == "ok"
    out_dump = d.dispatch(f"{s1} table dump MyIngress.ipv4_lpm")
    # decode_match renders match values in human-readable form: dotted-quad / CIDR.
    assert "10.0.0.5/32" in out_dump
    assert "10.0.0.2/32" in out_dump
    assert "set_egress_port" in out_dump
    # Action params decoded width-aware: port is 9-bit -> decimal.
    assert "'port': '2'" in out_dump


# ---------------------------------------------------------------------------
# 5. table del removes a row
# ---------------------------------------------------------------------------


def test_switch_table_del_removes_entry(network: Network) -> None:
    _h1, _h2, s1 = _names(network)
    d = CommandDispatcher(network, color=False)
    # Insert a fresh entry, then delete it, then assert dump no longer has it.
    d.dispatch(
        f"{s1} table add MyIngress.ipv4_lpm "
        "match: hdr.ipv4.dstAddr=10.0.0.6/32 "
        "action: MyIngress.set_egress_port "
        "params: port=2"
    )
    out_del = d.dispatch(f"{s1} table del MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.6/32")
    assert out_del == "ok"
    out_dump = d.dispatch(f"{s1} table dump MyIngress.ipv4_lpm")
    # Two pre-seeded entries (.1, .2) remain; the .6 we just deleted should be absent.
    assert "10.0.0.6" not in out_dump.replace(" ", "")
    # Use the bytes representation since dump renders canonical bytes.
    # b"\n\x00\x00\x06" is the canonical form of 10.0.0.6/32. Either rendering
    # is acceptable, so we just check the count of populated rows.
    assert out_dump.count("set_egress_port") == 2


def test_ternary_and_range_entry_forwards_and_deletes(tmp_path: Path) -> None:
    suffix = _suffix()
    h1, h2, s1 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(h1, mac="00:00:00:00:00:01")
    topo.add_host(h2, mac="00:00:00:00:00:02")
    with socket.socket() as grpc, socket.socket() as thrift:
        grpc.bind(("127.0.0.1", 0))
        thrift.bind(("127.0.0.1", 0))
        topo.add_switch(
            s1,
            _MATCH_TYPES,
            grpc_port=grpc.getsockname()[1],
            thrift_port=thrift.getsockname()[1],
        )
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(h2, s1, port_b=2)
    match = (
        "hdr.ethernet.dstAddr=00:00:00:00:00:02,"
        "hdr.ethernet.srcAddr=00:00:00:00:00:00&&&ff:ff:ff:ff:ff:00,"
        "hdr.ethernet.etherType=0x88b0..0x88bf"
    )
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    with Network(topo, compiler=compiler, log_dir=tmp_path / "logs") as net:
        d = CommandDispatcher(net)
        result = d.dispatch(
            f"{s1} table add MyIngress.l2_match match: {match} "
            "action: MyIngress.set_egress_port params: port=2 priority: 17"
        )
        assert result == "ok"
        dump = d.dispatch(f"{s1} table dump MyIngress.l2_match")
        assert "00:00:00:00:00:00&ff:ff:ff:ff:ff:00" in dump
        assert "[34992,35007]" in dump
        assert "priority: 17" in dump

        marker = uuid.uuid4().bytes
        frame = bytes.fromhex("00000000000200000000000188b5") + marker + bytes(48)
        ready, received = tmp_path / "ready", tmp_path / "received"
        receiver_script = (
            "import socket\n"
            "from pathlib import Path\n"
            "s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))\n"
            f"s.bind(({h2 + '-eth0'!r}, 0))\n"
            "s.settimeout(5)\n"
            f"Path({str(ready)!r}).write_text('ready')\n"
            "while True:\n"
            "    packet = s.recv(65535)\n"
            f"    if {marker!r} in packet:\n"
            f"        Path({str(received)!r}).write_bytes(packet)\n"
            "        break\n"
        )
        with net.host(h2).popen([sys.executable, "-c", receiver_script]) as receiver:
            deadline = time.monotonic() + 5
            while not ready.exists():
                assert time.monotonic() < deadline and receiver.poll() is None
                time.sleep(0.02)
            sender_script = (
                "import socket\n"
                "s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)\n"
                f"s.bind(({h1 + '-eth0'!r}, 0))\n"
                f"s.send({frame!r})\n"
            )
            net.host(h1).exec([sys.executable, "-c", sender_script], timeout=5)
            assert receiver.wait(timeout=7) == 0
        assert received.read_bytes() == frame
        assert d.dispatch(f"{s1} table del MyIngress.l2_match match: {match} priority: 17") == "ok"
        assert net.switch(s1).client.list_table_entries("MyIngress.l2_match") == []


# ---------------------------------------------------------------------------
# 6. counter read renders single-line text
# ---------------------------------------------------------------------------


def test_switch_counter_single_index(network: Network) -> None:
    _h1, _h2, s1 = _names(network)
    d = CommandDispatcher(network, color=False)
    # After test 2's pingall, port-2 cell is non-zero. We don't know the exact
    # number, but the format must be 'pkts=N bytes=M'.
    out = d.dispatch(f"{s1} counter MyIngress.ingress_pkts 0")
    assert out.startswith("pkts=")
    assert " bytes=" in out


# ---------------------------------------------------------------------------
# 7. multicast lifecycle
# ---------------------------------------------------------------------------


def test_switch_mcast_lifecycle(network: Network) -> None:
    _h1, _h2, s1 = _names(network)
    d = CommandDispatcher(network, color=False)
    out_add = d.dispatch(f"{s1} mcast add 1 1,2")
    assert out_add == "ok"
    out_list = d.dispatch(f"{s1} mcast list")
    assert "1: [1, 2]" in out_list
    out_del = d.dispatch(f"{s1} mcast del 1")
    assert out_del == "ok"
    out_list2 = d.dispatch(f"{s1} mcast list")
    assert "no multicast" in out_list2


# ---------------------------------------------------------------------------
# 8. host cmd ip -br addr returns 10.0.0.1/24
# ---------------------------------------------------------------------------


def test_host_cmd_ip_br_addr(network: Network) -> None:
    h1, _h2, _s1 = _names(network)
    d = CommandDispatcher(network, color=False)
    out = d.dispatch(f"{h1} cmd ip -br addr")
    assert "10.0.0.1/24" in out


def test_ping_numeric_validation_with_real_dual_stack_network(tmp_path: Path) -> None:
    suffix = _suffix()
    h1, h2, s1 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topology = Topology()
    topology.add_host(h1, ip="10.0.0.1/24", ip6="fd00:39::1/64")
    topology.add_host(h2, ip="10.0.0.2/24", ip6="fd00:39::2/64")
    with socket.socket() as grpc, socket.socket() as thrift:
        grpc.bind(("127.0.0.1", 0))
        thrift.bind(("127.0.0.1", 0))
        topology.add_switch(
            s1,
            _FIXTURES / "two_port_swap.p4",
            grpc_port=grpc.getsockname()[1],
            thrift_port=thrift.getsockname()[1],
        )
    topology.add_link(h1, s1, port_b=1)
    topology.add_link(h2, s1, port_b=2)
    with Network(
        topology, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as network:
        dispatcher = CommandDispatcher(network)
        commands = (f"{h1} ping {h2}", f"{h1} ping6 {h2}", "pingall", "pingall6")
        with ExitStack() as patches:
            launches = [
                patches.enter_context(
                    patch.object(
                        host.namespace, "popen", side_effect=AssertionError("invalid ping launched")
                    )
                )
                for host in network.hosts.values()
            ]
            for command in commands:
                for arguments in (
                    "0",
                    "-1",
                    "1 -0.25",
                    "1 nan",
                    "1 inf",
                    "2 1e308",
                    "1 2147482.25",
                ):
                    with pytest.raises(CLIUsageError):
                        dispatcher.dispatch(f"{command} {arguments}")
            for launch in launches:
                launch.assert_not_called()
        for command in commands:
            for timeout in ("0", "0.25"):
                output = dispatcher.dispatch(f"{command} 1 {timeout}")
                assert output == "OK" if command.startswith(h1) else "2/2 succeeded" in output
