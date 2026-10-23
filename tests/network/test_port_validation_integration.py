"""Legal host and switch port boundaries with real BMv2 forwarding."""

from __future__ import annotations

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

_SOURCE = Path(__file__).resolve().parents[1] / "fixtures/p4/match_types.p4"
_TABLE = "MyIngress.l2_match"
_ACTION = "MyIngress.set_egress_port"


@pytest.fixture
def network(tmp_path: Path, switch_port: int) -> Iterator[Network]:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.242.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(second, ip="10.242.0.2/24", mac="00:00:00:00:00:02")
    with socket.socket() as grpc, socket.socket() as thrift:
        grpc.bind(("127.0.0.1", 0))
        thrift.bind(("127.0.0.1", 0))
        topo.add_switch(
            switch,
            _SOURCE,
            cpu_port=255,
            grpc_port=grpc.getsockname()[1],
            thrift_port=thrift.getsockname()[1],
        )
    topo.add_link(first, switch, port_a=512, port_b=switch_port)
    topo.add_link(switch, second, port_a=7, port_b=0)
    topo = Topology.from_dict(topo.to_dict())
    with Network(
        topo, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as net:
        for sender, target, mac in (
            (first, "10.242.0.2", "00:00:00:00:00:02"),
            (second, "10.242.0.1", "00:00:00:00:00:01"),
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


@pytest.mark.parametrize("switch_port", [0, 510])
def test_port_boundaries_forward_after_round_trip(network: Network, switch_port: int) -> None:
    switch = next(iter(network.switches.values()))
    first, _second = network.hosts
    assert next(iter(network.host(first).interfaces)).endswith("-eth512")
    assert set(network.topology.port_assignments(switch.name)) == {switch_port, 7}
    client = switch.client
    routes = (("00:00:00:00:00:02", 7), ("00:00:00:00:00:01", switch_port))
    for mac, port in routes:
        client.insert_table_entry(
            _TABLE, {"hdr.ethernet.dstAddr": mac}, _ACTION, {"port": port}, priority=17
        )
    outcomes = network.pingall()
    assert len(outcomes) == 2 and all(outcomes.values())
    entries = client.list_table_entries(_TABLE)
    assert {int.from_bytes(entry["params"]["port"], "big") for entry in entries} == {switch_port, 7}
    for mac, _port in routes:
        client.delete_table_entry(_TABLE, {"hdr.ethernet.dstAddr": mac}, priority=17)
    assert client.list_table_entries(_TABLE) == []
