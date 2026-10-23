"""Integer switch IDs and service ports through a real BMv2 lifecycle."""

from __future__ import annotations

import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

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
def network(tmp_path: Path, device_id: int) -> Iterator[Network]:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.240.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(second, ip="10.240.0.2/24", mac="00:00:00:00:00:02")
    with socket.socket() as grpc, socket.socket() as thrift:
        grpc.bind(("127.0.0.1", 0))
        thrift.bind(("127.0.0.1", 0))
        topo.add_switch(
            switch,
            _SOURCE,
            device_id=device_id,
            grpc_port=grpc.getsockname()[1],
            thrift_port=thrift.getsockname()[1],
        )
    topo.add_link(first, switch, port_b=1)
    topo.add_link(second, switch, port_b=2)
    topo = Topology.from_dict(topo.to_dict())
    with Network(
        topo, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as net:
        for sender, target, mac in (
            (first, "10.240.0.2", "00:00:00:00:00:02"),
            (second, "10.240.0.1", "00:00:00:00:00:01"),
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


@pytest.mark.parametrize("device_id", [0, (1 << 31) - 1])
def test_switch_numbers_survive_round_trip_and_forward(network: Network, device_id: int) -> None:
    switch = next(iter(network.switches.values()))
    descriptor = switch.descriptor
    runtime = network._bmv2_switches[switch.name]
    client = switch.client
    assert descriptor.device_id == runtime.device_id == client.device_id == device_id
    for field in ("device_id", "grpc_port", "thrift_port"):
        value = getattr(descriptor, field)
        assert type(value) is int and getattr(runtime, field) == value
    for port in (descriptor.grpc_port, descriptor.thrift_port):
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    routes = (("10.240.0.1/32", 1), ("10.240.0.2/32", 2))
    with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
        for address, port in routes:
            client.insert_table_entry(
                _TABLE, {"hdr.ipv4.dstAddr": address}, _ACTION, {"port": port}
            )
        entries = client.list_table_entries(_TABLE)
        assert len(entries) == 2
        assert {int.from_bytes(row["params"]["port"], "big") for row in entries} == {1, 2}
        outcomes = network.pingall()
        assert len(outcomes) == 2 and all(outcomes.values())
        for address, _port in routes:
            client.delete_table_entry(_TABLE, {"hdr.ipv4.dstAddr": address})
        assert client.list_table_entries(_TABLE) == []
        assert rpc.call_count == 4
        assert all(call.args[0].device_id == device_id for call in rpc.call_args_list)
