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
    ep = LinkEndpoint(node="h1", port=0, iface_name="h1-eth0", ip="10.0.0.1/24")
    assert ep.node == "h1"
    assert ep.port == 0
    assert ep.iface_name == "h1-eth0"
    assert ep.ip == "10.0.0.1/24"


def test_link_a_must_be_linkendpoint() -> None:
    with pytest.raises(TopologyError, match="must be a LinkEndpoint"):
        Link(a="h1", b=LinkEndpoint(node="s1"))  # type: ignore[arg-type]
    with pytest.raises(TopologyError, match="must be a LinkEndpoint"):
        Link(a=LinkEndpoint(node="h1"), b="s1")  # type: ignore[arg-type]
