"""Address and gateway fields accept strings or None without coercion."""

from __future__ import annotations

import ipaddress
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from p4net.topo import Host, LinkEndpoint, Topology, TopologyError

_TARGETS = ("host_ip", "host_ip6", "default_route", "default_route6", "link_ip", "link_ip6")


def _bad_value(target: str, kind: str) -> object:
    address = ipaddress.ip_address("fd00:235::1" if target.endswith("6") else "10.235.0.1")
    values = {
        "int": int(address),
        "bool": True,
        "bytes": address.packed,
        "address": address,
        "interface": ipaddress.ip_interface(f"{address}/{64 if address.version == 6 else 24}"),
        "tuple": (str(address), 64 if address.version == 6 else 24),
        "float": 1.0,
        "list": [str(address)],
    }
    return values[kind]


def _host_arguments() -> dict[str, Any]:
    return {"ip": "10.235.0.1/24", "ip6": "fd00:235::1/64"}


def _field(target: str) -> str:
    return target.removeprefix("host_").removeprefix("link_")


@pytest.mark.parametrize("target", _TARGETS)
@pytest.mark.parametrize(
    "kind", ["int", "bool", "bytes", "address", "interface", "tuple", "float", "list"]
)
def test_address_descriptors_reject_non_strings(target: str, kind: str) -> None:
    field = _field(target)
    value = _bad_value(target, kind)
    with pytest.raises(TopologyError, match=f"{field} must be a string or None"):
        if target.startswith("link_"):
            LinkEndpoint(node="h1", **{field: value})
        else:
            arguments = _host_arguments()
            arguments[field] = value
            Host(name="h1", **arguments)


@pytest.mark.parametrize("target", _TARGETS)
@pytest.mark.parametrize("kind", ["int", "bool"])
def test_imported_non_string_addresses_are_rejected_without_mutation(
    target: str, kind: str
) -> None:
    topo = Topology()
    topo.add_host("h1", **_host_arguments())
    topo.add_switch("s1", Path("p.p4"))
    topo.add_link("h1", "s1")
    data = topo.to_dict()
    owner = data["links"][0]["a"] if target.startswith("link_") else data["hosts"]["h1"]
    owner[_field(target)] = _bad_value(target, kind)
    original = deepcopy(data)
    with pytest.raises(TopologyError, match=f"{_field(target)} must be a string or None"):
        Topology.from_dict(data)
    assert data == original


@pytest.mark.parametrize("target", _TARGETS)
def test_builders_reject_non_string_addresses_without_adding_nodes_or_links(target: str) -> None:
    topo = Topology()
    if target.startswith("link_"):
        topo.add_host("h1", **_host_arguments())
        topo.add_switch("s1", Path("p.p4"))
    before = topo.to_dict()
    value = _bad_value(target, "int")
    with pytest.raises(TopologyError, match=f"{_field(target)} must be a string or None"):
        if target.startswith("link_"):
            topo.add_link("h1", "s1", **{f"{_field(target)}_a": value})
        else:
            arguments = _host_arguments()
            arguments[_field(target)] = value
            topo.add_host("h1", **arguments)
    assert topo.to_dict() == before


@pytest.mark.parametrize("bare", [False, True])
@pytest.mark.parametrize("overrides", [False, True])
def test_valid_address_strings_keep_their_original_form(bare: bool, overrides: bool) -> None:
    ip = "10.235.0.1" + ("" if bare else "/24")
    ip6 = "FD00:0235:0000:0000:0000:0000:0000:0001" + ("" if bare else "/64")
    topo = Topology()
    topo.add_host("h1", ip=ip, ip6=ip6, default_route="10.235.0.254", default_route6="FD00:235::FF")
    topo.add_switch("s1", Path("p.p4"))
    topo.add_link("h1", "s1", **({"ip_a": ip, "ip6_a": ip6} if overrides else {}))
    data = topo.to_dict()
    restored = Topology.from_dict(data)
    restored.validate()
    assert restored.to_dict() == data
    assert restored.hosts["h1"].ip == ip and restored.hosts["h1"].ip6 == ip6
    assert ip in restored.to_graphviz() and ip6 in restored.to_graphviz()
    if overrides:
        assert restored.links[0].a.ip == ip and restored.links[0].a.ip6 == ip6


def test_address_fields_accept_none() -> None:
    host = Host("h1", ip=None, ip6=None, default_route=None, default_route6=None)
    endpoint = LinkEndpoint("h1", ip=None, ip6=None)
    assert host.ip is None and host.ip6 is None
    assert host.default_route is None and host.default_route6 is None
    assert endpoint.ip is None and endpoint.ip6 is None
