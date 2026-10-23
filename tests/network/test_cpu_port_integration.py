"""Reserve CPU ports while allocating physical links on a real BMv2 switch."""

from __future__ import annotations

import queue
import socket
import uuid
from collections.abc import Iterator
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

_SOURCE = Path(__file__).resolve().parents[1] / "fixtures/p4/simple_routing.p4"
_TABLE = "MyIngress.ipv4_lpm"
_ACTION = "MyIngress.set_egress_port"


@pytest.fixture
def network(tmp_path: Path, cpu_port: int) -> Iterator[Network]:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.243.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(second, ip="10.243.0.2/24", mac="00:00:00:00:00:02")
    with socket.socket() as grpc, socket.socket() as thrift:
        grpc.bind(("127.0.0.1", 0))
        thrift.bind(("127.0.0.1", 0))
        topo.add_switch(
            switch,
            _SOURCE,
            cpu_port=cpu_port,
            grpc_port=grpc.getsockname()[1],
            thrift_port=thrift.getsockname()[1],
        )
    topo.add_link(switch, first)
    topo.add_link(second, switch)
    with Network(
        topo, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as net:
        for sender, target, mac in (
            (first, "10.243.0.2", "00:00:00:00:00:02"),
            (second, "10.243.0.1", "00:00:00:00:00:01"),
        ):
            host = net.host(sender)
            host.exec(
                [
                    "ip",
                    "neigh",
                    "replace",
                    target,
                    "lladdr",
                    mac,
                    "dev",
                    next(iter(host.interfaces)),
                    "nud",
                    "permanent",
                ]
            )
        yield net


@pytest.mark.parametrize("cpu_port", [1, 2])
def test_reserved_cpu_port_keeps_forwarding_and_packet_in(network: Network, cpu_port: int) -> None:
    switch = next(iter(network.switches.values()))
    first, second = network.hosts
    assignments = network.topology.port_assignments(switch.name)
    assert cpu_port not in assignments
    ports = {endpoint.node: port for port, endpoint in assignments.items()}
    client = switch.client
    routes = ((first, "10.243.0.1/32"), (second, "10.243.0.2/32"))
    for name, address in routes:
        client.insert_table_entry(
            _TABLE, {"hdr.ipv4.dstAddr": address}, _ACTION, {"port": ports[name]}
        )
    assert all(network.pingall().values())

    marker = b"reserved-cpu-port-" + uuid.uuid4().bytes
    ip_header = (
        b"\x45\x00"
        + (20 + len(marker)).to_bytes(2, "big")
        + b"\x00\x00\x00\x00\x40\x11\x00\x00"
        + bytes([10, 243, 0, 1, 10, 243, 0, 2])
    )
    frame = bytes.fromhex("0000000000020000000000010800") + ip_header + marker
    packets: queue.Queue[bytes] = queue.Queue()
    unsubscribe = client.on_packet_in(
        lambda payload, metadata: packets.put(payload) if marker in payload else None
    )
    try:
        client.modify_table_entry(
            _TABLE, {"hdr.ipv4.dstAddr": "10.243.0.2/32"}, _ACTION, {"port": cpu_port}
        )
        host = network.host(first)
        script = (
            "import socket\n"
            "s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)\n"
            f"s.bind(({next(iter(host.interfaces))!r}, 0))\n"
            f"s.send(bytes.fromhex({frame.hex()!r}))\n"
            "s.close()\n"
        )
        host.exec(["python3", "-c", script], check=True)
        assert packets.get(timeout=5) == frame
    finally:
        unsubscribe()
    client.modify_table_entry(
        _TABLE, {"hdr.ipv4.dstAddr": "10.243.0.2/32"}, _ACTION, {"port": ports[second]}
    )
    assert all(network.pingall().values())
    for _name, address in routes:
        client.delete_table_entry(_TABLE, {"hdr.ipv4.dstAddr": address})
    assert client.list_table_entries(_TABLE) == []
