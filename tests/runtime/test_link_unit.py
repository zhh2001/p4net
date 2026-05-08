"""Unit tests for `p4net.runtime.link` (no privilege; pyroute2 mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from p4net.runtime import LinkError, NetworkNamespace, VethPair


@pytest.mark.parametrize(
    "bad_name",
    ["", " ", "has space", "has\ttab", "with/slash", "x" * 16],
)
def test_invalid_ifname_rejected(bad_name: str) -> None:
    with pytest.raises(ValueError):
        VethPair(bad_name, "vb")
    with pytest.raises(ValueError):
        VethPair("va", bad_name)


def test_same_names_rejected() -> None:
    with pytest.raises(ValueError):
        VethPair("v0", "v0")


def test_construct_valid() -> None:
    v = VethPair("va", "vb")
    assert v.name_a == "va"
    assert v.name_b == "vb"
    assert v.namespace_of("a") is None
    assert v.namespace_of("b") is None
    assert v.name_of("a") == "va"
    assert v.name_of("b") == "vb"
    assert "va" in repr(v) and "vb" in repr(v)


@pytest.mark.parametrize("bad_side", ["c", "A", "", "1", "left"])
def test_invalid_side_rejected(bad_side: str) -> None:
    v = VethPair("va", "vb")
    with pytest.raises(ValueError):
        v.namespace_of(bad_side)  # type: ignore[arg-type]


@pytest.fixture
def fake_ipr() -> MagicMock:
    ipr = MagicMock()
    ipr.__enter__.return_value = ipr
    ipr.__exit__.return_value = False
    ipr.link_lookup.return_value = []
    return ipr


@pytest.fixture
def patch_netlink(mocker: MockerFixture, fake_ipr: MagicMock) -> MagicMock:
    mocker.patch("p4net.runtime.link.IPRoute", return_value=fake_ipr)
    mocker.patch("p4net.runtime.link.NetNS", return_value=fake_ipr)
    return fake_ipr


def _force_created(v: VethPair) -> None:
    """Bypass create()/destroy() so we can exercise individual operations."""
    v._created = True


def test_create_calls_link_add(patch_netlink: MagicMock) -> None:
    patch_netlink.link_lookup.return_value = []
    v = VethPair("va", "vb")
    v.create()
    patch_netlink.link.assert_called_once_with("add", ifname="va", kind="veth", peer="vb")


def test_create_already_existing_iface_raises(patch_netlink: MagicMock) -> None:
    patch_netlink.link_lookup.return_value = [42]
    v = VethPair("va", "vb")
    with pytest.raises(LinkError):
        v.create()
    patch_netlink.link.assert_not_called()


def test_create_twice_raises(patch_netlink: MagicMock) -> None:
    patch_netlink.link_lookup.return_value = []
    v = VethPair("va", "vb")
    v.create()
    with pytest.raises(LinkError):
        v.create()


def test_destroy_before_create_raises() -> None:
    v = VethPair("va", "vb")
    with pytest.raises(LinkError):
        v.destroy()


def test_destroy_after_create_calls_link_del(patch_netlink: MagicMock) -> None:
    patch_netlink.link_lookup.return_value = []
    v = VethPair("va", "vb")
    v.create()
    patch_netlink.reset_mock()
    patch_netlink.link_lookup.return_value = [42]
    v.destroy()
    patch_netlink.link.assert_called_once_with("del", index=42)


def test_move_updates_namespace_state(patch_netlink: MagicMock, mocker: MockerFixture) -> None:
    patch_netlink.link_lookup.return_value = [10]
    mocker.patch("p4net.runtime.link.os.open", return_value=99)
    mocker.patch("p4net.runtime.link.os.close")
    v = VethPair("va", "vb")
    _force_created(v)
    target = NetworkNamespace("nsX")
    v.move_to_namespace("a", target)
    assert v.namespace_of("a") is target
    args = patch_netlink.link.call_args
    assert args.args[0] == "set"
    assert args.kwargs.get("net_ns_fd") == 99
    assert args.kwargs.get("index") == 10


def test_move_to_root_uses_pid1(patch_netlink: MagicMock) -> None:
    patch_netlink.link_lookup.return_value = [10]
    v = VethPair("va", "vb")
    _force_created(v)
    v.move_to_namespace("a", None)
    args = patch_netlink.link.call_args
    assert args.args[0] == "set"
    assert args.kwargs.get("net_ns_pid") == 1


def test_set_up_and_down(patch_netlink: MagicMock) -> None:
    patch_netlink.link_lookup.return_value = [10]
    v = VethPair("va", "vb")
    _force_created(v)
    v.set_up("a")
    assert patch_netlink.link.call_args.kwargs.get("state") == "up"
    v.set_down("b")
    assert patch_netlink.link.call_args.kwargs.get("state") == "down"


@pytest.mark.parametrize("bad_cidr", ["not.an.ip", "10.0.0.1/33", "", "10.0.0.300/24"])
def test_set_address_validates_cidr(patch_netlink: MagicMock, bad_cidr: str) -> None:
    v = VethPair("va", "vb")
    _force_created(v)
    with pytest.raises(ValueError):
        v.set_address("a", bad_cidr)


def test_set_address_calls_addr_add(patch_netlink: MagicMock) -> None:
    patch_netlink.link_lookup.return_value = [10]
    v = VethPair("va", "vb")
    _force_created(v)
    v.set_address("a", "10.0.0.1/24")
    patch_netlink.addr.assert_called_once_with("add", index=10, address="10.0.0.1", prefixlen=24)


@pytest.mark.parametrize("bad_mtu", [-1, 0, 67, 65536, 1_000_000])
def test_set_mtu_validates_range(patch_netlink: MagicMock, bad_mtu: int) -> None:
    v = VethPair("va", "vb")
    _force_created(v)
    with pytest.raises(ValueError):
        v.set_mtu("a", bad_mtu)


def test_set_mtu_rejects_non_int(patch_netlink: MagicMock) -> None:
    v = VethPair("va", "vb")
    _force_created(v)
    with pytest.raises(ValueError):
        v.set_mtu("a", "1500")  # type: ignore[arg-type]


def test_set_mtu_calls_link_set(patch_netlink: MagicMock) -> None:
    patch_netlink.link_lookup.return_value = [10]
    v = VethPair("va", "vb")
    _force_created(v)
    v.set_mtu("a", 1450)
    assert patch_netlink.link.call_args.kwargs.get("mtu") == 1450


@pytest.mark.parametrize(
    "bad_mac",
    [
        "",
        "aa:bb:cc:dd:ee",
        "aa:bb:cc:dd:ee:ff:gg",
        "aa-bb-cc-dd-ee-ff",
        "ZZ:ZZ:ZZ:ZZ:ZZ:ZZ",
    ],
)
def test_set_mac_validates(patch_netlink: MagicMock, bad_mac: str) -> None:
    v = VethPair("va", "vb")
    _force_created(v)
    with pytest.raises(ValueError):
        v.set_mac("a", bad_mac)


def test_set_mac_normalises_to_lower(patch_netlink: MagicMock) -> None:
    patch_netlink.link_lookup.return_value = [10]
    v = VethPair("va", "vb")
    _force_created(v)
    v.set_mac("a", "AA:BB:CC:DD:EE:FF")
    assert patch_netlink.link.call_args.kwargs.get("address") == "aa:bb:cc:dd:ee:ff"


def test_lookup_failure_raises_link_error(patch_netlink: MagicMock) -> None:
    patch_netlink.link_lookup.return_value = []
    v = VethPair("va", "vb")
    _force_created(v)
    with pytest.raises(LinkError):
        v.set_up("a")


def test_netns_handle_used_when_side_is_in_namespace(
    patch_netlink: MagicMock, mocker: MockerFixture
) -> None:
    """After moving side 'a' into nsZ, follow-up ops should target NetNS('nsZ')."""
    netns_factory = mocker.patch("p4net.runtime.link.NetNS", return_value=patch_netlink)
    mocker.patch("p4net.runtime.link.os.open", return_value=99)
    mocker.patch("p4net.runtime.link.os.close")
    patch_netlink.link_lookup.return_value = [10]
    v = VethPair("va", "vb")
    _force_created(v)
    target = NetworkNamespace("nsZ")
    v.move_to_namespace("a", target)
    netns_factory.reset_mock()
    v.set_up("a")
    netns_factory.assert_called_with("nsZ")
