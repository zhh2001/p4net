"""Preserve Unicode veth names in root and host network namespaces."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import uuid

import pytest
from pyroute2 import IPRoute
from pytest_mock import MockerFixture

from p4net.runtime import LinkError, NetworkNamespace, VethPair
from p4net.runtime.link import Side

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("placement", ["root", "one_namespace", "two_namespaces"])
def test_unicode_veth_lifecycle(placement: str) -> None:
    prefix = "u" + uuid.uuid4().hex[:7]
    names = (prefix + "a界界", prefix + "b界界")
    pair = VethPair(*names)
    namespaces: list[NetworkNamespace] = []
    try:
        pair.create()
        if udevadm := shutil.which("udevadm"):
            subprocess.run([udevadm, "settle", "--timeout=5"], check=True, timeout=6)
        with IPRoute() as ipr:
            assert all(ipr.link_lookup(ifname=name) for name in names)
        count = {"root": 0, "one_namespace": 1, "two_namespaces": 2}[placement]
        sides: tuple[Side, ...] = ("a", "b")
        for side in sides[:count]:
            ns = NetworkNamespace("ns" + uuid.uuid4().hex[:8])
            namespaces.append(ns)
            ns.create()
            pair.move_to_namespace(side, ns)
            result = ns.exec(
                ["ip", "-o", "link", "show", "dev", pair.name_of(side)], capture_output=True
            )
            assert pair.name_of(side).encode() in result.stdout
        pair.set_mtu("a", 1400)
        pair.set_mtu("b", 1400)
        pair.set_up("a")
        pair.set_up("b")
        pair.destroy()
        with IPRoute() as ipr:
            assert not any(
                link.get_attr("IFLA_IFNAME").startswith(prefix) for link in ipr.get_links()
            )
    finally:
        for ns in namespaces:
            with contextlib.suppress(Exception):
                ns.destroy()
        with IPRoute() as ipr:
            while owned := [
                link for link in ipr.get_links() if link.get_attr("IFLA_IFNAME").startswith(prefix)
            ]:
                ipr.link("del", index=owned[0]["index"])


def test_unicode_wait_failure_removes_real_interfaces(mocker: MockerFixture) -> None:
    token = uuid.uuid4()
    mocker.patch("p4net.runtime.link.uuid.uuid4", return_value=token)
    mocker.patch("p4net.runtime.link.Path").return_value.exists.return_value = True
    mocker.patch("shutil.which", return_value="/usr/bin/udevadm")
    mocker.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("udevadm", 6))
    prefix = "u" + token.hex[:7]
    names = (prefix + "a界界", prefix + "b界界")
    temporary = {"p4n" + token.hex[:9] + side for side in ("a", "b")}
    with IPRoute() as ipr:
        before = {link["index"] for link in ipr.get_links()}
        try:
            pair = VethPair(*names)
            with pytest.raises(LinkError, match="udev interface initialization"):
                pair.create()
            assert {link["index"] for link in ipr.get_links()} == before
        finally:
            while owned := [
                link
                for link in ipr.get_links()
                if link["index"] not in before and link.get_attr("IFLA_IFNAME") in temporary
            ]:
                ipr.link("del", index=owned[0]["index"])
