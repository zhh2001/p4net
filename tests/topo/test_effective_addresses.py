"""Check collisions against the addresses selected for host interfaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from p4net.topo import Topology, TopologyError


def _values(family: int) -> tuple[str, list[str]]:
    return (
        ("ip", [f"10.230.0.{number}/24" for number in range(1, 5)])
        if family == 4
        else ("ip6", [f"fd00:230::{number}/64" for number in range(1, 5)])
    )


def _check_snapshot(topo: Topology, collision: str | None = None) -> str:
    data = topo.to_dict()
    restored = Topology.from_dict(data)
    if collision is None:
        restored.validate()
        message = ""
    else:
        with pytest.raises(TopologyError, match=collision) as excinfo:
            restored.validate()
        message = str(excinfo.value)
    assert restored.to_dict() == data
    assert topo.to_dict() == data
    return message


@pytest.mark.parametrize("family", [4, 6])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("override_both", [False, True])
def test_overridden_primary_addresses_do_not_collide(
    family: int, reverse: bool, override_both: bool
) -> None:
    field, addresses = _values(family)
    topo = Topology()
    topo.add_host("h1", **{field: addresses[0]})
    topo.add_host("h2", **{field: addresses[0]})
    first, second = ("h2", "h1") if reverse else ("h1", "h2")
    side = "b" if reverse else "a"
    overrides = {f"{field}_{side}": addresses[1]}
    if override_both:
        overrides[f"{field}_{'a' if reverse else 'b'}"] = addresses[2]
    topo.add_link(first, second, **overrides)
    _check_snapshot(topo)


@pytest.mark.parametrize("family", [4, 6])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("source", ["defaults", "mixed", "overrides"])
def test_effective_address_collisions_still_rejected(
    family: int, reverse: bool, source: str
) -> None:
    field, addresses = _values(family)
    topo = Topology()
    topo.add_host("h1", **{field: addresses[0] if source == "defaults" else addresses[2]})
    topo.add_host("h2", **{field: addresses[0] if source != "overrides" else addresses[3]})
    first, second = ("h2", "h1") if reverse else ("h1", "h2")
    overrides = {}
    if source != "defaults":
        overrides[f"{field}_{'b' if reverse else 'a'}"] = addresses[0]
    if source == "overrides":
        overrides[f"{field}_{'a' if reverse else 'b'}"] = addresses[0]
    topo.add_link(first, second, **overrides)
    _check_snapshot(topo, "IP collision" if family == 4 else "IPv6 collision")


@pytest.mark.parametrize("family", [4, 6])
def test_collision_message_excludes_unused_defaults(family: int) -> None:
    field, addresses = _values(family)
    topo = Topology()
    topo.add_host("h1", **{field: addresses[0]})
    topo.add_host("h2", **{field: addresses[0]})
    topo.add_link("h1", "h2", **{f"{field}_a": addresses[1], f"{field}_b": addresses[1]})
    message = _check_snapshot(topo, "collision")
    assert addresses[0].split("/")[0] not in message
    assert addresses[1].split("/")[0] in message


@pytest.mark.parametrize("family", [4, 6])
@pytest.mark.parametrize("reverse", [False, True])
def test_first_override_consumes_default_address(family: int, reverse: bool) -> None:
    field, addresses = _values(family)
    topo = Topology()
    topo.add_host("h1", **{field: addresses[0]})
    topo.add_host("h2", **{field: addresses[0]})
    topo.add_switch("s1", Path("p.p4"))
    topo.add_switch("s2", Path("p.p4"))
    topo.add_link("s1", "h1", **{f"{field}_b": addresses[1]}) if reverse else topo.add_link(
        "h1", "s1", **{f"{field}_a": addresses[1]}
    )
    topo.add_link("h1", "s2")
    topo.add_link("h2", "s2")
    _check_snapshot(topo)


@pytest.mark.parametrize("family", [4, 6])
def test_later_override_does_not_hide_inherited_default(family: int) -> None:
    field, addresses = _values(family)
    topo = Topology()
    topo.add_host("h1", **{field: addresses[0]})
    topo.add_host("h2", **{field: addresses[0]})
    topo.add_switch("s1", Path("p.p4"))
    topo.add_switch("s2", Path("p.p4"))
    topo.add_link("h1", "s1")
    topo.add_link("h1", "s2", **{f"{field}_a": addresses[1]})
    topo.add_link("h2", "s1")
    _check_snapshot(topo, "collision")


@pytest.mark.parametrize("override_family", [4, 6])
def test_overriding_one_family_keeps_the_other_family_collision(override_family: int) -> None:
    field, addresses = _values(override_family)
    topo = Topology()
    for name in ("h1", "h2"):
        topo.add_host(name, ip="10.230.0.1/24", ip6="fd00:230::1/64")
    topo.add_link("h1", "h2", **{f"{field}_a": addresses[1]})
    remaining = "IPv6 collision" if override_family == 4 else "IP collision"
    message = _check_snapshot(topo, remaining)
    assert ("\n  - IP collision" if override_family == 4 else "IPv6 collision") not in message


@pytest.mark.parametrize("family", [4, 6])
@pytest.mark.parametrize("connected", [False, True])
def test_unlinked_host_defaults_do_not_participate(family: int, connected: bool) -> None:
    field, addresses = _values(family)
    topo = Topology()
    topo.add_host("h1", **{field: addresses[0]})
    topo.add_host("h2", **{field: addresses[0]})
    if connected:
        topo.add_switch("s1", Path("p.p4"))
        topo.add_link("h2", "s1")
    _check_snapshot(topo)


@pytest.mark.parametrize("family", [4, 6])
def test_same_host_address_on_multiple_interfaces_remains_allowed(family: int) -> None:
    field, addresses = _values(family)
    topo = Topology()
    topo.add_host("h1", **{field: addresses[0]})
    topo.add_switch("s1", Path("p.p4"))
    topo.add_switch("s2", Path("p.p4"))
    topo.add_link("h1", "s1")
    topo.add_link("h1", "s2", **{f"{field}_a": addresses[0]})
    _check_snapshot(topo)
