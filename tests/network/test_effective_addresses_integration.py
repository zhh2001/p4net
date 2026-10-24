"""Validate and configure the same effective IPv4 and IPv6 host addresses."""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from p4net import Network
from p4net.compiler import P4Compiler
from p4net.topo import Topology, TopologyError

pytestmark = pytest.mark.integration
_SOURCE = Path(__file__).resolve().parents[1] / "fixtures/p4/simple_routing.p4"


def _addresses(family: int) -> tuple[str, str, str, str]:
    return (
        ("ip", "10.230.0.1/24", "10.230.0.2/24", "10.230.0.3/24")
        if family == 4
        else ("ip6", "fd00:230::1/64", "fd00:230::2/64", "fd00:230::3/64")
    )


def _configured_addresses(network: Network, host: str, family: int) -> set[str]:
    output = network.host(host).exec(["ip", "-j", "addr", "show"], capture_output=True)
    return {
        address["local"]
        for link in json.loads(output.stdout)
        for address in link.get("addr_info", [])
        if link["ifname"] != "lo"
        and address["family"] == ("inet" if family == 4 else "inet6")
        and address.get("scope") == "global"
    }


@pytest.mark.parametrize("family", [4, 6])
@pytest.mark.parametrize("layout", ["override", "later_link", "unlinked", "different_prefixes"])
def test_effective_addresses_match_kernel_configuration(
    tmp_path: Path, family: int, layout: str
) -> None:
    field, primary, override, third_ip = _addresses(family)
    suffix = uuid.uuid4().hex[:6]
    first, second, third = f"h{suffix}a", f"h{suffix}b", f"h{suffix}c"
    topo = Topology()
    topo.add_host(first, **{field: primary})
    topo.add_host(second, **{field: primary})
    if layout == "different_prefixes":
        override = override.split("/")[0] + ("/16" if family == 4 else "/48")
    if layout in {"override", "different_prefixes"}:
        topo.add_link(first, second, **{f"{field}_a": override})
    elif layout == "later_link":
        topo.add_host(third, **{field: third_ip})
        topo.add_link(first, third, **{f"{field}_a": override})
        topo.add_link(first, second)
    before = topo.to_dict()
    topo.validate()
    with Network(topo, log_dir=tmp_path / "logs") as network:
        expected_first = set() if layout == "unlinked" else {override.split("/")[0]}
        assert _configured_addresses(network, first, family) == expected_first
        assert _configured_addresses(network, second, family) == (
            set() if layout == "unlinked" else {primary.split("/")[0]}
        )
        if layout == "unlinked":
            assert not network.host(first).interfaces and not network.host(first).interfaces6
            assert (
                network.host(first).primary_ip is None and network.host(first).primary_ip6 is None
            )
        else:
            peer, peer_ip = (
                (second, primary)
                if layout in {"override", "different_prefixes"}
                else (third, third_ip)
            )
            for sender, target in ((first, peer_ip), (peer, override)):
                command = [
                    "ping",
                    *(["-6"] if family == 6 else []),
                    "-c",
                    "1",
                    "-W",
                    "2",
                    target.split("/")[0],
                ]
                assert network.host(sender).exec(command, check=False).returncode == 0
        assert topo.to_dict() == before


@pytest.mark.parametrize("family", [4, 6])
def test_effective_collisions_rejected_before_compilation(tmp_path: Path, family: int) -> None:
    field, primary, override, _ = _addresses(family)
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, **{field: primary})
    topo.add_host(second, **{field: override})
    topo.add_switch(switch, _SOURCE)
    topo.add_link(first, switch, **{f"{field}_a": override})
    topo.add_link(second, switch)
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    logs = tmp_path / "logs"
    network = Network(topo, compiler=compiler, log_dir=logs)
    try:
        with patch.object(compiler, "compile", wraps=compiler.compile) as compile_call:
            with pytest.raises(TopologyError, match="collision"):
                network.start()
            compile_call.assert_not_called()
        assert not logs.exists()
    finally:
        network.stop()


@pytest.mark.parametrize("family", [4, 6])
@pytest.mark.parametrize("source", ["defaults", "overrides"])
def test_different_prefix_collisions_rejected_before_compilation(
    tmp_path: Path, family: int, source: str
) -> None:
    field, primary, unused_first, unused_second = _addresses(family)
    wide = primary.split("/")[0] + ("/16" if family == 4 else "/48")
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, **{field: primary if source == "defaults" else unused_first})
    topo.add_host(second, **{field: wide if source == "defaults" else unused_second})
    topo.add_switch(switch, _SOURCE)
    topo.add_link(first, switch, **({f"{field}_a": primary} if source == "overrides" else {}))
    topo.add_link(switch, second, **({f"{field}_b": wide} if source == "overrides" else {}))
    before = topo.to_dict()
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    logs = tmp_path / "logs"
    network = Network(topo, compiler=compiler, log_dir=logs)
    try:
        with patch.object(compiler, "compile", wraps=compiler.compile) as compile_call:
            with pytest.raises(TopologyError, match="collision"):
                network.start()
            compile_call.assert_not_called()
        assert not logs.exists()
        assert topo.to_dict() == before
    finally:
        network.stop()


@pytest.mark.requires_p4c
@pytest.mark.requires_bmv2
@pytest.mark.parametrize("wide_prefix", [False, True])
def test_overridden_ipv4_defaults_forward_through_bmv2(tmp_path: Path, wide_prefix: bool) -> None:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.230.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(second, ip="10.230.0.1/24", mac="00:00:00:00:00:02")
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
    topo.add_link(first, switch, port_a=0, port_b=0, ip_a=f"10.230.0.2/{16 if wide_prefix else 24}")
    topo.add_link(switch, second, port_a=2, port_b=0)
    before = topo.to_dict()
    topo.validate()
    with Network(
        topo, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as network:
        assert network.host(first).primary_ip == "10.230.0.2"
        assert network.host(second).primary_ip == "10.230.0.1"
        for sender, target, mac in (
            (first, "10.230.0.1", "00:00:00:00:00:02"),
            (second, "10.230.0.2", "00:00:00:00:00:01"),
        ):
            host = network.host(sender)
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
        client = network.switch(switch).client
        routes = (("10.230.0.2/32", 0), ("10.230.0.1/32", 2))
        for address, port in routes:
            client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": address},
                "MyIngress.set_egress_port",
                {"port": port},
            )
        result = network.pingall()
        assert len(result) == 2 and all(result.values())
        for address, _ in routes:
            client.delete_table_entry("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": address})
        assert client.list_table_entries("MyIngress.ipv4_lpm") == []
        assert topo.to_dict() == before
