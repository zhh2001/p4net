"""Unit tests for `p4net.topo.host`."""

from __future__ import annotations

import dataclasses

import pytest

from p4net.topo import Host, TopologyError


def test_minimal_host() -> None:
    h = Host(name="h1")
    assert h.name == "h1"
    assert h.ip is None
    assert h.mac is None
    assert h.default_route is None


def test_full_host() -> None:
    h = Host(name="h1", ip="10.0.0.1/24", mac="aa:bb:cc:dd:ee:ff", default_route="10.0.0.254")
    assert h.ip == "10.0.0.1/24"
    assert h.mac == "aa:bb:cc:dd:ee:ff"
    assert h.default_route == "10.0.0.254"


def test_host_is_frozen() -> None:
    h = Host(name="h1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.name = "h2"  # type: ignore[misc]


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "1host",  # leads with digit
        "_host",  # leads with underscore
        "-host",  # leads with hyphen
        "host with space",
        "host/slash",
        "host.dot",
        "x" * 13,  # too long
    ],
)
def test_invalid_host_name(bad_name: str) -> None:
    with pytest.raises(TopologyError):
        Host(name=bad_name)


@pytest.mark.parametrize(
    "good_name",
    ["h", "h1", "host_a", "host-a", "Host1", "abcdefghijkl"],  # 12 chars
)
def test_valid_host_name(good_name: str) -> None:
    Host(name=good_name)


@pytest.mark.parametrize(
    "bad_ip",
    ["not.an.ip", "10.0.0.1/33", "10.0.0.300/24", "fe80::1/64"],
)
def test_invalid_host_ip(bad_ip: str) -> None:
    with pytest.raises(TopologyError):
        Host(name="h1", ip=bad_ip)


def test_host_ip_without_mask_accepted() -> None:
    """IPv4Interface accepts a bare address as /32."""
    h = Host(name="h1", ip="10.0.0.1")
    assert h.ip == "10.0.0.1"


@pytest.mark.parametrize(
    "bad_mac",
    ["", "aa:bb:cc:dd:ee", "aa-bb-cc-dd-ee-ff", "ZZ:ZZ:ZZ:ZZ:ZZ:ZZ"],
)
def test_invalid_host_mac(bad_mac: str) -> None:
    with pytest.raises(TopologyError):
        Host(name="h1", mac=bad_mac)


def test_default_route_requires_ip() -> None:
    with pytest.raises(TopologyError, match="requires ip"):
        Host(name="h1", default_route="10.0.0.254")


@pytest.mark.parametrize("bad_route", ["not.an.ip", "10.0.0.254/24", "fe80::1"])
def test_invalid_default_route(bad_route: str) -> None:
    with pytest.raises(TopologyError):
        Host(name="h1", ip="10.0.0.1/24", default_route=bad_route)


# ---------------------------------------------------------------------------
# IPv6 (phase 12)
# ---------------------------------------------------------------------------


def test_host_ip6_accepted() -> None:
    h = Host(name="h1", ip6="fd00::1/64")
    assert h.ip6 == "fd00::1/64"


@pytest.mark.parametrize(
    "bad_ip6",
    ["10.0.0.1/24", "fd00::g/64", "fd00::1/129", "not::v6", ""],
)
def test_invalid_host_ip6(bad_ip6: str) -> None:
    with pytest.raises(TopologyError):
        Host(name="h1", ip6=bad_ip6)


def test_default_route6_requires_ip6() -> None:
    with pytest.raises(TopologyError, match="requires ip6"):
        Host(name="h1", default_route6="fd00::ff")


@pytest.mark.parametrize("bad_route6", ["not::v6", "fd00::1/64", "10.0.0.1"])
def test_invalid_default_route6(bad_route6: str) -> None:
    with pytest.raises(TopologyError):
        Host(name="h1", ip6="fd00::1/64", default_route6=bad_route6)
