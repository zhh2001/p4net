"""Unit tests for `p4net.runtime.link` (no privilege; pyroute2 mocked)."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, call

import pytest
from pytest_mock import MockerFixture

from p4net.runtime import LinkError, NetworkNamespace, VethPair


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        " ",
        "has space",
        "has\ttab",
        "with/slash",
        "x" * 16,
        False,
        0,
        1.5,
        [],
        {},
        b"eth0",
        ".",
        "..",
        "with:colon",
        "nul\0tail",
        "界" * 6,
        "\ud800",
    ],
)
def test_invalid_ifname_rejected(bad_name: Any) -> None:
    with pytest.raises(ValueError):
        VethPair(bad_name, "vb")
    with pytest.raises(ValueError):
        VethPair("va", bad_name)


def test_same_names_rejected() -> None:
    with pytest.raises(ValueError):
        VethPair("v0", "v0")


@pytest.mark.parametrize("name", ["v", "x" * 15, "界" * 5, "名" + "x" * 12])
def test_valid_interface_names_preserved(name: str) -> None:
    first = VethPair(name, "peer")
    second = VethPair("peer", name)
    assert first.name_a == name
    assert second.name_b == name


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


@pytest.mark.parametrize("names", [("甲", "乙"), ("甲", "vb"), ("va", "乙")])
def test_unicode_create_waits_before_assigning_names(
    patch_netlink: MagicMock, mocker: MockerFixture, names: tuple[str, str]
) -> None:
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("shutil.which", return_value="/usr/bin/udevadm")
    wait = mocker.patch("subprocess.run")
    indexes: dict[str, int] = {}
    events: list[str] = []

    def lookup(*, ifname: str) -> list[int]:
        return [indexes[ifname]] if ifname in indexes else []

    def link(command: str, **kwargs: Any) -> None:
        if command == "add":
            assert kwargs["ifname"].isascii() and kwargs["peer"].isascii()
            indexes.update({kwargs["ifname"]: 41, kwargs["peer"]: 42})
            events.append("create")
        elif command == "set":
            events.append("rename")

    patch_netlink.link_lookup.side_effect = lookup
    patch_netlink.link.side_effect = link
    wait.side_effect = lambda *args, **kwargs: events.append("wait")
    pair = VethPair(*names)
    pair.create()
    assert events == ["create", "wait", "rename", "rename"]
    assert patch_netlink.link.call_args_list[-2:] == [
        call("set", index=41, ifname=names[0]),
        call("set", index=42, ifname=names[1]),
    ]
    wait.assert_called_once()


@pytest.mark.parametrize("failure", ["wait", "wait_error", "rename_a", "rename_b"])
def test_unicode_create_failure_deletes_pair_by_index(
    patch_netlink: MagicMock, mocker: MockerFixture, failure: str
) -> None:
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("shutil.which", return_value="/usr/bin/udevadm")
    wait = mocker.patch("subprocess.run")
    if failure == "wait":
        wait.side_effect = subprocess.TimeoutExpired("udevadm", 6)
    elif failure == "wait_error":
        wait.side_effect = subprocess.CalledProcessError(1, "udevadm")
    patch_netlink.link_lookup.side_effect = [[], [], [41], [42]]

    def link(command: str, **kwargs: Any) -> None:
        if command == "set" and kwargs["index"] == (41 if failure == "rename_a" else 42):
            raise OSError("rename failed")

    patch_netlink.link.side_effect = link
    pair = VethPair("甲", "乙")
    with pytest.raises((LinkError, OSError)):
        pair.create()
    assert patch_netlink.link.call_args_list[-1] == call("del", index=41)
    assert not pair._created


@pytest.mark.parametrize("missing_side", ["a", "b"])
def test_unicode_create_lookup_failure_removes_created_pair(
    patch_netlink: MagicMock, mocker: MockerFixture, missing_side: str
) -> None:
    mocker.patch("pathlib.Path.exists", return_value=False)
    patch_netlink.link_lookup.side_effect = (
        [[], [], []]
        if missing_side == "a"
        else [
            [],
            [],
            [41],
            [],
        ]
    )
    pair = VethPair("甲", "乙")
    with pytest.raises(LinkError, match="not found"):
        pair.create()
    temporary_a = patch_netlink.link.call_args_list[0].kwargs["ifname"]
    assert patch_netlink.link.call_args_list[-1] == (
        call("del", ifname=temporary_a) if missing_side == "a" else call("del", index=41)
    )
    assert not pair._created


def test_unicode_create_without_udev_needs_no_command(
    patch_netlink: MagicMock, mocker: MockerFixture
) -> None:
    mocker.patch("pathlib.Path.exists", return_value=False)
    which = mocker.patch("shutil.which")
    wait = mocker.patch("subprocess.run")
    patch_netlink.link_lookup.side_effect = [[], [], [41], [42]]
    VethPair("甲", "乙").create()
    which.assert_not_called()
    wait.assert_not_called()
    assert patch_netlink.link.call_args_list[-1] == call("set", index=42, ifname="乙")


def test_unicode_create_missing_udevadm_rejected_before_creation(
    patch_netlink: MagicMock, mocker: MockerFixture
) -> None:
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("shutil.which", return_value=None)
    patch_netlink.link_lookup.return_value = []
    with pytest.raises(LinkError, match="udevadm"):
        VethPair("甲", "乙").create()
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


@pytest.mark.parametrize(
    "bad_cidr",
    ["not::an::ip", "fd00::1/129", "fd00::ggg", ""],
)
def test_set_address6_validates_cidr(patch_netlink: MagicMock, bad_cidr: str) -> None:
    v = VethPair("va", "vb")
    _force_created(v)
    with pytest.raises(ValueError):
        v.set_address6("a", bad_cidr)


def test_set_address6_rejects_ipv4_string(patch_netlink: MagicMock) -> None:
    v = VethPair("va", "vb")
    _force_created(v)
    with pytest.raises(ValueError, match="IPv4-shaped"):
        v.set_address6("a", "10.0.0.1/24")


def test_set_address6_calls_addr_add_with_v6_family(patch_netlink: MagicMock) -> None:
    import socket as _socket

    patch_netlink.link_lookup.return_value = [10]
    v = VethPair("va", "vb")
    _force_created(v)
    v.set_address6("a", "fd00::1/64")
    patch_netlink.addr.assert_called_once_with(
        "add",
        index=10,
        address="fd00::1",
        prefixlen=64,
        family=_socket.AF_INET6,
    )


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
