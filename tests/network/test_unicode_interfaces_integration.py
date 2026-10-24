"""Preserve Unicode interface names through real BMv2 forwarding and shutdown."""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

import pytest
from pyroute2 import IPRoute

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


@pytest.mark.parametrize("unicode_side", ["host", "switch", "both"])
def test_unicode_interfaces_preserve_names_and_forward(tmp_path: Path, unicode_side: str) -> None:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.236.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(second, ip="10.236.0.2/24", mac="00:00:00:00:00:02")
    with socket.socket() as grpc, socket.socket() as thrift:
        grpc.bind(("127.0.0.1", 0))
        thrift.bind(("127.0.0.1", 0))
        topo.add_switch(
            switch,
            _SOURCE,
            cpu_port=1,
            grpc_port=grpc.getsockname()[1],
            thrift_port=thrift.getsockname()[1],
        )
    topo.add_link(first, switch, port_a=0, port_b=0)
    topo.add_link(switch, second, port_a=2, port_b=0)
    complete = topo.to_dict()
    names = (
        (f"{suffix}-host-a00", f"{suffix}-sw-a0000"),
        (f"{suffix}-sw-b0000", f"{suffix}-host-b00"),
    )
    if unicode_side != "switch":
        names = ((f"{suffix}a界界00", names[0][1]), (names[1][0], f"{suffix}d界界00"))
    if unicode_side != "host":
        names = ((names[0][0], f"{suffix}b界界00"), (f"{suffix}c界界00", names[1][1]))
    for link, pair in zip(complete["links"], names, strict=True):
        for side, name in zip(("a", "b"), pair, strict=True):
            assert len(name.encode("utf-8")) == 15
            link[side]["iface_name"] = name
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    restored = Topology.from_dict(complete)
    restored.validate()
    assert restored.to_dict() == complete
    try:
        with Network(restored, compiler=compiler, log_dir=tmp_path / "valid-logs") as network:
            for sender, target, mac, interface in (
                (first, "10.236.0.2", "00:00:00:00:00:02", names[0][0]),
                (second, "10.236.0.1", "00:00:00:00:00:01", names[1][1]),
            ):
                host = network.host(sender)
                assert set(host.interfaces) == {interface}
                host.exec(
                    [
                        "ip",
                        "neigh",
                        "replace",
                        target,
                        "lladdr",
                        mac,
                        "dev",
                        interface,
                        "nud",
                        "permanent",
                    ]
                )
            client = network.switch(switch).client
            routes = (("10.236.0.1/32", 0), ("10.236.0.2/32", 2))
            assert set(network.topology.port_assignments(switch)) == {0, 2}
            for address, port in routes:
                client.insert_table_entry(
                    _TABLE, {"hdr.ipv4.dstAddr": address}, _ACTION, {"port": port}
                )
            outcomes = network.pingall()
            assert len(outcomes) == 2 and all(outcomes.values())
            for address, _port in routes:
                client.delete_table_entry(_TABLE, {"hdr.ipv4.dstAddr": address})
            assert client.list_table_entries(_TABLE) == []
        with IPRoute() as ipr:
            assert not any(
                link.get_attr("IFLA_IFNAME").startswith(suffix) for link in ipr.get_links()
            )
    finally:
        with IPRoute() as ipr:
            while owned := [
                link for link in ipr.get_links() if link.get_attr("IFLA_IFNAME").startswith(suffix)
            ]:
                ipr.link("del", index=owned[0]["index"])
