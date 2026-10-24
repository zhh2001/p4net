"""Reject switch endpoint addresses before creating network resources."""

from __future__ import annotations

import json
import socket
import subprocess
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from p4net import Network
from p4net.compiler import P4Compiler
from p4net.topo import Topology, TopologyError

pytestmark = pytest.mark.integration
_SOURCE = Path(__file__).resolve().parents[1] / "fixtures/p4/simple_routing.p4"


def _topology(switch_first: bool) -> tuple[Topology, str, str, str]:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, mac="00:00:00:00:00:01")
    topo.add_host(second, mac="00:00:00:00:00:02")
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
    host_side = "b" if switch_first else "a"
    for number, host, port in ((1, first, 0), (2, second, 2)):
        topo.add_link(
            *((switch, host) if switch_first else (host, switch)),
            **{
                f"port_{host_side}": 0,
                f"port_{'a' if switch_first else 'b'}": port,
                f"ip_{host_side}": f"10.235.0.{number}/24",
                f"ip6_{host_side}": f"fd00:235::{number}/64",
            },
        )
    return topo, first, second, switch


@pytest.mark.parametrize("fields", [("ip",), ("ip6",), ("ip", "ip6")])
@pytest.mark.parametrize("switch_first", [False, True])
def test_imported_switch_addresses_rejected_before_compilation(
    tmp_path: Path, fields: tuple[str, ...], switch_first: bool
) -> None:
    topo, _first, _second, _switch = _topology(switch_first)
    data = topo.to_dict()
    side = "a" if switch_first else "b"
    addresses = {"ip": "10.234.0.1/24", "ip6": "fd00:234::1/64"}
    for field in fields:
        data["links"][0][side][field] = addresses[field]
    restored = Topology.from_dict(data)
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    logs = tmp_path / "logs"
    network = Network(restored, compiler=compiler, log_dir=logs)
    try:
        with patch.object(compiler, "compile", wraps=compiler.compile) as compile_call:
            with pytest.raises(TopologyError, match="P4 switch data ports do not carry IP"):
                network.start()
            compile_call.assert_not_called()
        assert not logs.exists()
        assert restored.to_dict() == data
    finally:
        network.stop()


@pytest.mark.requires_p4c
@pytest.mark.requires_bmv2
@pytest.mark.parametrize("state", ["missing", "null"])
@pytest.mark.parametrize("switch_first", [False, True])
def test_imported_unset_switch_addresses_preserve_host_configuration_and_forward(
    tmp_path: Path, state: str, switch_first: bool
) -> None:
    topo, first, second, switch = _topology(switch_first)
    data = topo.to_dict()
    side = "a" if switch_first else "b"
    if state == "missing":
        for link in data["links"]:
            for field in ("ip", "ip6"):
                link[side].pop(field)
    original = deepcopy(data)
    restored = Topology.from_dict(data)
    before = restored.to_dict()
    restored.validate()
    assert restored.to_dict() == before and data == original
    with Network(
        restored, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as network:
        for number, name in ((1, first), (2, second)):
            host = network.host(name)
            assert host.primary_ip == f"10.235.0.{number}"
            assert host.primary_ip6 == f"fd00:235::{number}"
            interfaces = json.loads(
                host.exec(["ip", "-j", "addr", "show"], capture_output=True).stdout
            )
            actual = {
                (address["family"], address["local"], address["prefixlen"])
                for interface in interfaces
                for address in interface.get("addr_info", [])
                if interface["ifname"] != "lo" and address.get("scope") == "global"
            }
            assert actual == {
                ("inet", f"10.235.0.{number}", 24),
                ("inet6", f"fd00:235::{number}", 64),
            }
            host.exec(
                [
                    "ip",
                    "neigh",
                    "replace",
                    f"10.235.0.{3 - number}",
                    "lladdr",
                    f"00:00:00:00:00:0{3 - number}",
                    "dev",
                    next(iter(host.interfaces)),
                    "nud",
                    "permanent",
                ]
            )
        for link in data["links"]:
            interface = link[side]["iface_name"]
            (result,) = json.loads(
                subprocess.check_output(["ip", "-j", "addr", "show", "dev", interface], text=True)
            )
            assert result.get("addr_info", []) == []
        client = network.switch(switch).client
        routes = (("10.235.0.1/32", 0), ("10.235.0.2/32", 2))
        for address, port in routes:
            client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": address},
                "MyIngress.set_egress_port",
                {"port": port},
            )
        outcomes = network.pingall()
        assert len(outcomes) == 2 and all(outcomes.values())
        for address, _port in routes:
            client.delete_table_entry("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": address})
        assert client.list_table_entries("MyIngress.ipv4_lpm") == []
        assert restored.to_dict() == before and data == original
