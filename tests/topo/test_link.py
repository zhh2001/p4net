"""Unit tests for `p4net.topo.link`."""

from __future__ import annotations

import dataclasses
from copy import deepcopy
from typing import Any

import pytest

from p4net.topo import Link, LinkEndpoint, Topology, TopologyError

_LOSS_FIELDS = (
    "loss_pct",
    "loss_pct_a_to_b",
    "loss_pct_b_to_a",
    "loss_pct_a_to_b_extra",
    "loss_pct_b_to_a_extra",
)


def _make_loss_link(field: str, value: Any, source: str) -> Link:
    values = {field: value}
    if field.endswith("_extra"):
        values["loss_pct"] = 0
    if source == "descriptor":
        return Link(LinkEndpoint("h1"), LinkEndpoint("h2"), **values)
    topo = Topology()
    topo.add_host("h1")
    topo.add_host("h2")
    if source == "builder":
        before = topo.to_dict()
        try:
            return topo.add_link("h1", "h2", **values)
        except Exception:
            assert topo.to_dict() == before
            raise
    topo.add_link("h1", "h2")
    data = topo.to_dict()
    data["links"][0].update(values)
    original = deepcopy(data)
    try:
        restored = Topology.from_dict(data)
        restored.validate()
        return restored.links[0]
    finally:
        assert data == original


@pytest.mark.parametrize("source", ["descriptor", "builder", "dictionary"])
@pytest.mark.parametrize("field", _LOSS_FIELDS)
@pytest.mark.parametrize(
    "value",
    [True, False, "1", b"1", [], {}, 1 + 0j, float("nan"), float("inf"), -float("inf"), 10**400],
)
def test_loss_fields_reject_invalid_types_and_nonfinite_values(
    source: str, field: str, value: Any
) -> None:
    with pytest.raises(TopologyError, match=rf"{field}.*finite"):
        _make_loss_link(field, value, source)


@pytest.mark.parametrize("source", ["descriptor", "builder", "dictionary"])
@pytest.mark.parametrize("field", _LOSS_FIELDS)
@pytest.mark.parametrize("value", [None, 0, 0.5, 100])
def test_loss_fields_preserve_finite_numbers_and_none(
    source: str, field: str, value: float | None
) -> None:
    actual = getattr(_make_loss_link(field, value, source), field)
    assert actual == value
    assert type(actual) is type(value)


@pytest.mark.parametrize("source", ["descriptor", "builder", "dictionary"])
@pytest.mark.parametrize(
    ("field", "value"),
    [(field, value) for field in _LOSS_FIELDS for value in (-0.5,)]
    + [(field, 100.5) for field in _LOSS_FIELDS[:3]],
)
def test_loss_fields_keep_existing_ranges(source: str, field: str, value: float) -> None:
    with pytest.raises(TopologyError, match=field):
        _make_loss_link(field, value, source)


@pytest.mark.parametrize("source", ["descriptor", "builder", "dictionary"])
@pytest.mark.parametrize("field", _LOSS_FIELDS[3:])
def test_loss_extra_keeps_existing_sum_validation_stage(source: str, field: str) -> None:
    assert getattr(_make_loss_link(field, 101, source), field) == 101


@pytest.mark.parametrize(
    "port", [True, False, -1, 1.0, 1.5, "1", b"1", float("nan"), float("inf"), [], {}]
)
def test_endpoint_rejects_invalid_port_number(port: Any) -> None:
    with pytest.raises(TopologyError, match="port"):
        LinkEndpoint(node="h1", port=port)


@pytest.mark.parametrize("port", [None, 0, 1, 510, 511, 512, 10000])
def test_endpoint_preserves_optional_and_nonnegative_ports(port: int | None) -> None:
    assert LinkEndpoint(node="h1", port=port).port == port


def test_minimal_link() -> None:
    link = Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="s1"))
    assert link.a.node == "h1"
    assert link.b.node == "s1"
    assert link.bandwidth is None
    assert link.delay is None


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("node", [None, False, True, 0, 1, 1.5, b"h1", ["h1"], {"name": "h1"}, ""])
def test_link_requires_nonempty_string_node_references(side: str, node: Any) -> None:
    endpoints = {"a": LinkEndpoint("h1"), "b": LinkEndpoint("s1")}
    endpoints[side] = LinkEndpoint(node=node)
    with pytest.raises(TopologyError, match=rf"Link\.{side}\.node must be a non-empty string"):
        Link(**endpoints)


@pytest.mark.parametrize("node", ["h1", "Host_1-z", "not_present_yet"])
def test_link_keeps_string_node_references_without_resolving_them(node: str) -> None:
    link = Link(a=LinkEndpoint(node), b=LinkEndpoint("s1"))
    assert link.a.node == node


def test_link_is_frozen() -> None:
    link = Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="s1"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        link.bandwidth = "10mbit"  # type: ignore[misc]


def test_link_jitter_requires_delay() -> None:
    with pytest.raises(TopologyError, match="jitter requires delay"):
        Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="s1"), jitter="2ms")


def test_link_jitter_with_delay_ok() -> None:
    link = Link(
        a=LinkEndpoint(node="h1"),
        b=LinkEndpoint(node="s1"),
        delay="10ms",
        jitter="2ms",
    )
    assert link.delay == "10ms"
    assert link.jitter == "2ms"


@pytest.mark.parametrize("bad_loss", [-0.1, 100.1, -1.0, 200.0])
def test_link_invalid_loss_pct(bad_loss: float) -> None:
    with pytest.raises(TopologyError):
        Link(
            a=LinkEndpoint(node="h1"),
            b=LinkEndpoint(node="s1"),
            loss_pct=bad_loss,
        )


@pytest.mark.parametrize("good_loss", [0.0, 0.5, 50.0, 100.0])
def test_link_valid_loss_pct(good_loss: float) -> None:
    Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="s1"), loss_pct=good_loss)


@pytest.mark.parametrize("bad_mtu", [-1, 0, 67, 65536, 1_000_000])
def test_link_invalid_mtu(bad_mtu: int) -> None:
    with pytest.raises(TopologyError):
        Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="s1"), mtu=bad_mtu)


@pytest.mark.parametrize("source", ["descriptor", "builder", "dictionary"])
@pytest.mark.parametrize(
    "mtu",
    [
        True,
        False,
        1500.0,
        1500.5,
        "1500",
        b"1500",
        float("nan"),
        float("inf"),
        -float("inf"),
        [],
        {},
    ],
)
def test_mtu_rejects_invalid_types_without_changing_topology(source: str, mtu: Any) -> None:
    topo = Topology()
    topo.add_host("h1")
    topo.add_host("h2")
    before = topo.to_dict()
    data = {"links": [{"a": {"node": "h1"}, "b": {"node": "h2"}, "mtu": mtu}]}
    original = deepcopy(data)
    with pytest.raises(TopologyError, match=r"mtu.*int"):
        if source == "descriptor":
            Link(LinkEndpoint("h1"), LinkEndpoint("h2"), mtu=mtu)
        elif source == "builder":
            topo.add_link("h1", "h2", mtu=mtu)
        else:
            Topology.from_dict(data)
    assert topo.to_dict() == before
    assert data == original


@pytest.mark.parametrize("source", ["descriptor", "builder", "dictionary"])
@pytest.mark.parametrize("mtu", [None, 68, 1500, 65535, "default"])
def test_mtu_preserves_optional_integers_and_default(source: str, mtu: Any) -> None:
    values = {} if mtu == "default" else {"mtu": mtu}
    expected = None if mtu == "default" else mtu
    topo = Topology()
    topo.add_host("h1")
    topo.add_host("h2")
    if source == "descriptor":
        link = Link(LinkEndpoint("h1"), LinkEndpoint("h2"), **values)
    elif source == "builder":
        link = topo.add_link("h1", "h2", **values)
    else:
        topo.add_link("h1", "h2")
        data = topo.to_dict()
        if mtu == "default":
            del data["links"][0]["mtu"]
        else:
            data["links"][0]["mtu"] = mtu
        original = deepcopy(data)
        restored = Topology.from_dict(data)
        restored.validate()
        assert data == original
        link = restored.links[0]
        assert restored.to_dict()["links"][0]["mtu"] == expected
    assert link.mtu == expected
    assert link.mtu is None or type(link.mtu) is int


@pytest.mark.parametrize("source", ["builder", "dictionary"])
@pytest.mark.parametrize("mtu", [-1, 0, 67, 65536, 1_000_000])
def test_mtu_keeps_existing_range_validation(source: str, mtu: int) -> None:
    topo = Topology()
    topo.add_host("h1")
    topo.add_host("h2")
    if source == "builder":
        with pytest.raises(TopologyError, match="out of range"):
            topo.add_link("h1", "h2", mtu=mtu)
        assert not topo.links
    else:
        topo.add_link("h1", "h2")
        data = topo.to_dict()
        data["links"][0]["mtu"] = mtu
        original = deepcopy(data)
        with pytest.raises(TopologyError, match="out of range"):
            Topology.from_dict(data)
        assert data == original


def test_link_endpoint_node_must_be_non_empty() -> None:
    with pytest.raises(TopologyError, match="non-empty"):
        Link(a=LinkEndpoint(node=""), b=LinkEndpoint(node="s1"))
    with pytest.raises(TopologyError, match="non-empty"):
        Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node=""))


def test_link_endpoint_full_fields() -> None:
    ep = LinkEndpoint(
        node="h1",
        port=0,
        iface_name="h1-eth0",
        ip="10.0.0.1/24",
        mac="aa:bb:cc:dd:ee:ff",
    )
    assert ep.node == "h1"
    assert ep.port == 0
    assert ep.iface_name == "h1-eth0"
    assert ep.ip == "10.0.0.1/24"
    assert ep.mac == "aa:bb:cc:dd:ee:ff"


def test_link_endpoint_mac_unset_passes_through() -> None:
    ep = LinkEndpoint(node="h1")
    assert ep.mac is None


def test_link_endpoint_valid_mac_accepted() -> None:
    ep = LinkEndpoint(node="h1", mac="00:11:22:33:44:55")
    assert ep.mac == "00:11:22:33:44:55"


@pytest.mark.parametrize(
    "bad_mac",
    [
        "",
        "aa:bb:cc:dd:ee",
        "aa:bb:cc:dd:ee:ff:gg",
        "aa-bb-cc-dd-ee-ff",
        "ZZ:ZZ:ZZ:ZZ:ZZ:ZZ",
        "aa:bb:cc:dd:ee:fff",
    ],
)
def test_link_endpoint_invalid_mac_rejected(bad_mac: str) -> None:
    with pytest.raises(TopologyError, match="invalid LinkEndpoint MAC"):
        LinkEndpoint(node="h1", mac=bad_mac)


def test_link_a_must_be_linkendpoint() -> None:
    with pytest.raises(TopologyError, match="must be a LinkEndpoint"):
        Link(a="h1", b=LinkEndpoint(node="s1"))  # type: ignore[arg-type]
    with pytest.raises(TopologyError, match="must be a LinkEndpoint"):
        Link(a=LinkEndpoint(node="h1"), b="s1")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Asymmetric impairment + IPv6 endpoint (phase 12)
# ---------------------------------------------------------------------------


def test_link_endpoint_ip6_accepted() -> None:
    ep = LinkEndpoint(node="h1", ip6="fd00::1/64")
    assert ep.ip6 == "fd00::1/64"


@pytest.mark.parametrize(
    "bad_ip6",
    ["10.0.0.1/24", "fd00::g/64", "fd00::1/129", "not::v6"],
)
def test_link_endpoint_invalid_ip6(bad_ip6: str) -> None:
    with pytest.raises(TopologyError):
        LinkEndpoint(node="h1", ip6=bad_ip6)


def test_link_asymmetric_per_direction_fields() -> None:
    link = Link(
        a=LinkEndpoint(node="h1"),
        b=LinkEndpoint(node="s1"),
        delay_a_to_b="100ms",
        loss_pct_b_to_a=5.0,
    )
    assert link.delay_a_to_b == "100ms"
    assert link.loss_pct_b_to_a == 5.0


@pytest.mark.parametrize(
    ("kwargs", "param"),
    [
        ({"bandwidth": "10mbit", "bandwidth_a_to_b": "5mbit"}, "bandwidth"),
        ({"delay": "10ms", "delay_b_to_a": "20ms"}, "delay"),
        ({"jitter": "1ms", "delay": "10ms", "jitter_a_to_b": "2ms"}, "jitter"),
        ({"loss_pct": 1.0, "loss_pct_a_to_b": 2.0}, "loss_pct"),
    ],
)
def test_link_symmetric_and_asymmetric_collide(kwargs: dict, param: str) -> None:
    with pytest.raises(TopologyError, match=f"both {param}"):
        Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="s1"), **kwargs)


def test_link_jitter_a_to_b_requires_delay() -> None:
    with pytest.raises(TopologyError, match="jitter_a_to_b requires"):
        Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="s1"), jitter_a_to_b="1ms")


def test_link_jitter_a_to_b_satisfied_by_symmetric_delay() -> None:
    link = Link(
        a=LinkEndpoint(node="h1"),
        b=LinkEndpoint(node="s1"),
        delay="10ms",
        jitter_a_to_b="1ms",
    )
    assert link.jitter_a_to_b == "1ms"


def test_link_jitter_b_to_a_requires_delay() -> None:
    with pytest.raises(TopologyError, match="jitter_b_to_a requires"):
        Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="s1"), jitter_b_to_a="1ms")


@pytest.mark.parametrize("bad_loss", [-0.1, 100.5, 200.0])
def test_link_loss_pct_a_to_b_range(bad_loss: float) -> None:
    with pytest.raises(TopologyError, match="loss_pct_a_to_b"):
        Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="s1"), loss_pct_a_to_b=bad_loss)


@pytest.mark.parametrize("bad_loss", [-0.1, 100.5, 200.0])
def test_link_loss_pct_b_to_a_range(bad_loss: float) -> None:
    with pytest.raises(TopologyError, match="loss_pct_b_to_a"):
        Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="s1"), loss_pct_b_to_a=bad_loss)


def test_link_delay_extra_requires_symmetric_base() -> None:
    with pytest.raises(TopologyError, match="delay_a_to_b_extra requires symmetric delay"):
        Link(
            a=LinkEndpoint(node="h1"),
            b=LinkEndpoint(node="s1"),
            delay_a_to_b_extra="50ms",
        )


def test_link_delay_extra_conflicts_with_per_direction() -> None:
    with pytest.raises(TopologyError, match="both delay_a_to_b and delay_a_to_b_extra"):
        Link(
            a=LinkEndpoint(node="h1"),
            b=LinkEndpoint(node="s1"),
            delay_a_to_b="200ms",
            delay_a_to_b_extra="50ms",
        )


def test_link_jitter_extra_requires_symmetric_base() -> None:
    with pytest.raises(TopologyError, match="jitter_a_to_b_extra requires symmetric jitter"):
        Link(
            a=LinkEndpoint(node="h1"),
            b=LinkEndpoint(node="s1"),
            delay="10ms",
            jitter_a_to_b_extra="1ms",
        )


def test_link_loss_pct_extra_requires_symmetric_base() -> None:
    with pytest.raises(TopologyError, match="loss_pct_b_to_a_extra requires symmetric loss_pct"):
        Link(
            a=LinkEndpoint(node="h1"),
            b=LinkEndpoint(node="s1"),
            loss_pct_b_to_a_extra=5.0,
        )


def test_link_loss_pct_extra_negative_rejected() -> None:
    with pytest.raises(TopologyError, match=r"loss_pct_a_to_b_extra .* non-negative"):
        Link(
            a=LinkEndpoint(node="h1"),
            b=LinkEndpoint(node="s1"),
            loss_pct=1.0,
            loss_pct_a_to_b_extra=-0.5,
        )


def test_link_delay_extra_accepted() -> None:
    link = Link(
        a=LinkEndpoint(node="h1"),
        b=LinkEndpoint(node="s1"),
        delay="100ms",
        delay_a_to_b_extra="50ms",
    )
    assert link.delay == "100ms"
    assert link.delay_a_to_b_extra == "50ms"
    assert link.delay_b_to_a_extra is None
