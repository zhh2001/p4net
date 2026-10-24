"""Reuse interface names across namespaces without depending on link order."""

from __future__ import annotations

import socket
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
from pyroute2 import IPRoute

from p4net import Network
from p4net.compiler import P4Compiler
from p4net.topo import Topology, TopologyError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_p4c,
    pytest.mark.requires_bmv2,
]

_SOURCE = Path(__file__).resolve().parents[1] / "fixtures/p4/simple_routing.p4"
_TABLE = "MyIngress.ipv4_lpm"
_ACTION = "MyIngress.set_egress_port"


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("kind", ["hosts_only", "cross_links", "all_scopes"])
@pytest.mark.parametrize("alphabet", ["ascii", "utf8"])
def test_interface_name_reuse_forwards_in_either_link_order(
    tmp_path: Path, reverse: bool, kind: str, alphabet: str
) -> None:
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
    shared = suffix + ("shared" if alphabet == "ascii" else "a界界00")
    names = ((shared, suffix + "s1"), (suffix + "s2", shared))
    if kind == "cross_links":
        names = ((suffix + "h1", shared), (suffix + "s2", shared))
    elif kind == "all_scopes":
        names = ((shared, shared), (suffix + "s2", shared))
    for link, pair in zip(complete["links"], names, strict=True):
        for side, name in zip(("a", "b"), pair, strict=True):
            assert len(name.encode("utf-8")) <= 15
            link[side]["iface_name"] = name
    if reverse:
        complete["links"].reverse()
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


@pytest.mark.parametrize("conflict", ["host", "root", "loopback"])
def test_interface_name_conflict_rejected_before_real_compilation(
    tmp_path: Path, conflict: str
) -> None:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first)
    topo.add_host(second)
    topo.add_switch(switch, _SOURCE)
    topo.add_link(first, switch)
    topo.add_link(second, switch)
    data = topo.to_dict()
    if conflict == "host":
        data["links"][1]["a"].update(node=first, port=1)
        for link in data["links"]:
            link["a"]["iface_name"] = suffix + "same"
    elif conflict == "root":
        for link in data["links"]:
            link["b"]["iface_name"] = suffix + "same"
    else:
        data["links"][0]["a"]["iface_name"] = "lo"
    restored = Topology.from_dict(data)
    before = deepcopy(restored.to_dict())
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    logs = tmp_path / "logs"
    network = Network(restored, compiler=compiler, log_dir=logs)
    try:
        with patch.object(compiler, "compile", wraps=compiler.compile) as compile_call:
            with pytest.raises(
                TopologyError, match="loopback" if conflict == "loopback" else "collision"
            ):
                network.start()
            compile_call.assert_not_called()
        assert not logs.exists()
        assert restored.to_dict() == before
    finally:
        network.stop()
