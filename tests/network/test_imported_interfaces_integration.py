"""Validate endpoint interface snapshots before starting a real network."""

from __future__ import annotations

import socket
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

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


@pytest.mark.parametrize("missing_side", ["host", "switch"])
def test_imported_interfaces_reject_missing_and_preserve_custom_names(
    tmp_path: Path, missing_side: str
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
    names = (
        (f"{suffix}-host-a00", f"{suffix}-sw-a0000"),
        (f"{suffix}-sw-b0000", f"{suffix}-host-b00"),
    )
    for link, pair in zip(complete["links"], names, strict=True):
        for side, name in zip(("a", "b"), pair, strict=True):
            assert len(name) == 15
            link[side]["iface_name"] = name
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    for missing in ("omitted", "null", "empty"):
        data = deepcopy(complete)
        endpoint = data["links"][0]["a" if missing_side == "host" else "b"]
        if missing == "omitted":
            endpoint.pop("iface_name")
        else:
            endpoint["iface_name"] = None if missing == "null" else ""
        incomplete = Topology.from_dict(data)
        before = incomplete.to_dict()
        bad_logs = tmp_path / f"invalid-{missing}-logs"
        invalid = Network(incomplete, compiler=compiler, log_dir=bad_logs)
        try:
            with patch.object(compiler, "compile", wraps=compiler.compile) as compile_call:
                with pytest.raises(TopologyError, match="has no assigned interface name"):
                    invalid.start()
                compile_call.assert_not_called()
            assert not bad_logs.exists()
            assert incomplete.to_dict() == before
        finally:
            invalid.stop()

    restored = Topology.from_dict(complete)
    restored.validate()
    assert restored.to_dict() == complete
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
