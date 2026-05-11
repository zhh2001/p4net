"""Unit tests for `p4net.topo.link`."""

from __future__ import annotations

import dataclasses

import pytest

from p4net.topo import Link, LinkEndpoint, TopologyError


def test_minimal_link() -> None:
    link = Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="s1"))
    assert link.a.node == "h1"
    assert link.b.node == "s1"
    assert link.bandwidth is None
    assert link.delay is None


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
