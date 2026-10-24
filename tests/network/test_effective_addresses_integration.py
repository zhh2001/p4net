"""Validate and configure the same effective IPv4 and IPv6 host addresses."""

from __future__ import annotations

import ipaddress
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
@pytest.mark.parametrize(
    "layout", ["override", "later_link", "unlinked", "different_prefixes", "routes"]
)
def test_effective_addresses_match_kernel_configuration(
    tmp_path: Path, family: int, layout: str
) -> None:
    field, primary, override, third_ip = _addresses(family)
    suffix = uuid.uuid4().hex[:6]
    first, second, third = f"h{suffix}a", f"h{suffix}b", f"h{suffix}c"
    topo = Topology()
    route = "default_route" if family == 4 else "default_route6"
    topo.add_host(
        first, **{field: primary}, **({route: primary.split("/")[0]} if layout == "routes" else {})
    )
    topo.add_host(
        second,
        **{field: primary},
        **({route: override.split("/")[0]} if layout == "routes" else {}),
    )
    if layout == "different_prefixes":
        override = override.split("/")[0] + ("/16" if family == 4 else "/48")
    if layout in {"override", "different_prefixes", "routes"}:
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
                if layout in {"override", "different_prefixes", "routes"}
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
            if layout == "routes":
                for host, gateway in ((first, primary), (second, override)):
                    command = [
                        "ip",
                        *(["-6"] if family == 6 else []),
                        "-j",
                        "route",
                        "show",
                        "default",
                    ]
                    routes = json.loads(
                        network.host(host).exec(command, capture_output=True).stdout
                    )
                    assert len(routes) == 1 and routes[0]["gateway"] == gateway.split("/")[0]
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
@pytest.mark.parametrize("mac_overrides", [False, True])
def test_overridden_ipv4_defaults_forward_through_bmv2(
    tmp_path: Path, wide_prefix: bool, mac_overrides: bool
) -> None:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.230.0.1/24", mac="00:00:00:00:00:01", default_route="10.230.0.1")
    topo.add_host(second, ip="10.230.0.1/24", mac="00:00:00:00:00:02", default_route="10.230.0.2")
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
    first_mac = "02:AA:bb:CC:dd:01" if mac_overrides else "00:00:00:00:00:01"
    second_mac = "02:aa:BB:cc:DD:02" if mac_overrides else "00:00:00:00:00:02"
    switch_macs = ("02:AB:cd:EF:00:01", "02:ab:CD:ef:00:02")
    topo.add_link(
        first,
        switch,
        port_a=0,
        port_b=0,
        ip_a=f"10.230.0.2/{16 if wide_prefix else 24}",
        mac_a=first_mac if mac_overrides else None,
        mac_b=switch_macs[0] if mac_overrides else None,
    )
    topo.add_link(
        switch,
        second,
        port_a=2,
        port_b=0,
        mac_a=switch_macs[1] if mac_overrides else None,
        mac_b=second_mac if mac_overrides else None,
    )
    before = topo.to_dict()
    topo.validate()
    with Network(
        topo, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as network:
        assert network.host(first).primary_ip == "10.230.0.2"
        assert network.host(second).primary_ip == "10.230.0.1"
        if mac_overrides:
            for name, mac in ((first, first_mac), (second, second_mac)):
                host = network.host(name)
                interface = next(iter(host.interfaces))
                result = json.loads(
                    host.exec(
                        ["ip", "-j", "link", "show", "dev", interface], capture_output=True
                    ).stdout
                )
                assert result[0]["address"] == mac.lower()
            for endpoint, mac in (
                (topo.links[0].b, switch_macs[0]),
                (topo.links[1].a, switch_macs[1]),
            ):
                result = json.loads(
                    subprocess.check_output(
                        ["ip", "-j", "link", "show", "dev", endpoint.iface_name], text=True
                    )
                )
                assert result[0]["address"] == mac.lower()
        for sender, target, mac in (
            (first, "10.230.0.1", second_mac),
            (second, "10.230.0.2", first_mac),
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


@pytest.mark.parametrize(
    "target", ["host_ip", "host_ip6", "default_route", "default_route6", "link_ip", "link_ip6"]
)
def test_imported_integer_addresses_rejected_before_startup(tmp_path: Path, target: str) -> None:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.230.0.1/24", ip6="fd00:230::1/64")
    topo.add_host(second, ip="10.230.0.2/24", ip6="fd00:230::2/64")
    topo.add_switch(switch, _SOURCE)
    topo.add_link(first, switch)
    topo.add_link(second, switch)
    data = topo.to_dict()
    field = target.removeprefix("host_").removeprefix("link_")
    owner = data["links"][0]["a"] if target.startswith("link_") else data["hosts"][first]
    owner[field] = int(
        ipaddress.ip_address("fd00:230::1" if target.endswith("6") else "10.230.0.1")
    )
    original = deepcopy(data)
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    logs = tmp_path / "logs"
    with patch.object(compiler, "compile", wraps=compiler.compile) as compile_call:
        with pytest.raises(TopologyError, match=f"{field} must be a string or None"):
            restored = Topology.from_dict(data)
            with Network(restored, compiler=compiler, log_dir=logs):
                pass
        compile_call.assert_not_called()
    assert not logs.exists()
    assert data == original


@pytest.mark.parametrize("target", ["host", "host_endpoint", "switch_endpoint"])
@pytest.mark.parametrize("switch_first", [False, True])
def test_imported_integer_macs_rejected_before_startup(
    tmp_path: Path, target: str, switch_first: bool
) -> None:
    suffix = uuid.uuid4().hex[:6]
    first, switch = f"h{suffix}", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.230.0.1/24")
    topo.add_switch(switch, _SOURCE)
    topo.add_link(*((switch, first) if switch_first else (first, switch)))
    data = topo.to_dict()
    if target == "host":
        owner = data["hosts"][first]
    else:
        side = "a" if (target == "switch_endpoint") == switch_first else "b"
        owner = data["links"][0][side]
    owner["mac"] = 17
    original = deepcopy(data)
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    logs = tmp_path / "logs"
    with patch.object(compiler, "compile", wraps=compiler.compile) as compile_call:
        with pytest.raises(TopologyError, match=r"invalid.*MAC"):
            restored = Topology.from_dict(data)
            with Network(restored, compiler=compiler, log_dir=logs):
                pass
        compile_call.assert_not_called()
    assert not logs.exists()
    assert data == original
