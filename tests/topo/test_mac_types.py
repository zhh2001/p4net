"""MAC fields reject invalid types without changing accepted strings."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from p4net.topo import Host, LinkEndpoint, Topology, TopologyError


@pytest.mark.parametrize("descriptor", ["host", "endpoint"])
@pytest.mark.parametrize(
    "value",
    [
        17,
        0,
        True,
        False,
        1.5,
        b"02:aa:bb:cc:dd:01",
        ["02:aa:bb:cc:dd:01"],
        {"mac": "02:aa:bb:cc:dd:01"},
    ],
)
def test_mac_descriptors_reject_non_strings(descriptor: str, value: object) -> None:
    with pytest.raises(TopologyError, match=r"invalid.*MAC"):
        if descriptor == "host":
            Host("h1", mac=value)  # type: ignore[arg-type]
        else:
            LinkEndpoint("h1", mac=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("target", ["host", "host_endpoint", "switch_endpoint"])
def test_builders_reject_mac_types_without_adding_descriptions(target: str) -> None:
    topo = Topology()
    if target != "host":
        topo.add_host("h1")
        topo.add_switch("s1", Path("p.p4"))
    before = topo.to_dict()
    with pytest.raises(TopologyError, match=r"invalid.*MAC"):
        if target == "host":
            topo.add_host("h1", mac=17)  # type: ignore[arg-type]
        else:
            field = "mac_a" if target == "host_endpoint" else "mac_b"
            topo.add_link("h1", "s1", **{field: 17})
    assert topo.to_dict() == before


@pytest.mark.parametrize("target", ["host", "host_endpoint", "switch_endpoint"])
@pytest.mark.parametrize("switch_first", [False, True])
@pytest.mark.parametrize("value", [17, b"02:aa:bb:cc:dd:01"])
def test_imported_mac_types_rejected_without_changing_input(
    target: str, switch_first: bool, value: object
) -> None:
    topo = Topology()
    topo.add_host("h1")
    topo.add_switch("s1", Path("p.p4"))
    topo.add_link(*(("s1", "h1") if switch_first else ("h1", "s1")))
    data = topo.to_dict()
    if target == "host":
        owner = data["hosts"]["h1"]
    else:
        side = "a" if (target == "switch_endpoint") == switch_first else "b"
        owner = data["links"][0][side]
    owner["mac"] = value
    original = deepcopy(data)
    with pytest.raises(TopologyError, match=r"invalid.*MAC"):
        Topology.from_dict(data)
    assert data == original


@pytest.mark.parametrize("descriptor", ["host", "endpoint"])
@pytest.mark.parametrize(
    "mac", [None, "02:aa:bb:cc:dd:01", "02:AA:BB:CC:DD:01", "02:AA:bb:CC:dd:01"]
)
def test_mac_descriptors_preserve_case_and_none(descriptor: str, mac: str | None) -> None:
    value = Host("h1", mac=mac) if descriptor == "host" else LinkEndpoint("h1", mac=mac)
    assert value.mac == mac


@pytest.mark.parametrize("switch_first", [False, True])
def test_imported_mac_strings_preserve_case_on_both_endpoint_types(switch_first: bool) -> None:
    topo = Topology()
    topo.add_host("h1", mac="02:AA:BB:CC:DD:01")
    topo.add_switch("s1", Path("p.p4"))
    host_side, switch_side = ("b", "a") if switch_first else ("a", "b")
    topo.add_link(
        *(("s1", "h1") if switch_first else ("h1", "s1")),
        **{f"mac_{host_side}": "02:AA:bb:CC:dd:02", f"mac_{switch_side}": "02:ab:CD:ef:00:01"},
    )
    data = topo.to_dict()
    restored = Topology.from_dict(data)
    restored.validate()
    assert restored.to_dict() == data
