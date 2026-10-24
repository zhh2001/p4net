"""Reuse a veth object across destruction, creation failures, and retries."""

from __future__ import annotations

import contextlib
import json
import uuid
from unittest.mock import patch

import pytest
from pyroute2 import IPRoute
from pyroute2.netlink.exceptions import NetlinkError

from p4net.runtime import LinkError, NetworkNamespace, VethPair

pytestmark = pytest.mark.integration


def _assert_forwarding(pair: VethPair, sender: NetworkNamespace) -> None:
    pair.set_address("a", "10.232.0.1/24")
    pair.set_address("b", "10.232.0.2/24")
    pair.set_up("a")
    pair.set_up("b")
    assert sender.exec(["ping", "-c", "1", "-W", "2", "10.232.0.2"]).returncode == 0


@pytest.mark.parametrize("placement", ["root", "targets"])
@pytest.mark.parametrize("alphabet", ["ascii", "utf8"])
def test_veth_recreation_preserves_creation_targets(placement: str, alphabet: str) -> None:
    prefix = uuid.uuid4().hex[:6]
    names = (
        (prefix + "a", prefix + "b")
        if alphabet == "ascii"
        else (prefix + "a界界00", prefix + "b界界00")
    )
    namespaces = [NetworkNamespace("ns" + uuid.uuid4().hex[:8]) for _ in range(2)]
    pair = VethPair(
        *names,
        namespace_a=namespaces[0] if placement == "targets" else None,
        namespace_b=namespaces[1] if placement == "targets" else None,
    )
    try:
        for ns in namespaces:
            ns.create()
            ns.exec(["ip", "link", "set", "lo", "up"])
        for _ in range(2):
            pair.create()
            if placement == "root":
                with IPRoute() as ipr:
                    assert all(ipr.link_lookup(ifname=name) for name in names)
                assert pair.namespace_of("a") is None and pair.namespace_of("b") is None
                pair.move_to_namespace("a", namespaces[0])
                pair.move_to_namespace("b", namespaces[1])
            else:
                assert pair.namespace_of("a") is namespaces[0]
                assert pair.namespace_of("b") is namespaces[1]
            _assert_forwarding(pair, namespaces[0])
            pair.destroy()
            assert pair.namespace_of("a") is None and pair.namespace_of("b") is None
    finally:
        for ns in namespaces:
            with contextlib.suppress(Exception):
                ns.destroy()
        with IPRoute() as ipr:
            while owned := [
                link for link in ipr.get_links() if link.get_attr("IFLA_IFNAME").startswith(prefix)
            ]:
                ipr.link("del", index=owned[0]["index"])


def test_failed_recreation_preserves_foreign_interfaces_and_can_retry() -> None:
    prefix = uuid.uuid4().hex[:6]
    name = prefix + "shared"
    token = uuid.uuid4()
    temporary_prefix = "p4n" + token.hex[:9]
    namespaces = [NetworkNamespace("ns" + uuid.uuid4().hex[:8]) for _ in range(2)]
    pair = VethPair(name, name, namespace_a=namespaces[0], namespace_b=namespaces[1])
    try:
        for ns in namespaces:
            ns.create()
            ns.exec(["ip", "link", "set", "lo", "up"])
        pair.create()
        pair.destroy()
        original = []
        for ns in namespaces:
            ns.exec(["ip", "link", "add", "name", name, "type", "dummy"])
            original.append(
                json.loads(ns.exec(["ip", "-j", "link", "show"], capture_output=True).stdout)
            )
        with IPRoute() as ipr:
            before = {link["index"] for link in ipr.get_links()}
        with (
            patch("p4net.runtime.link.uuid.uuid4", return_value=token),
            pytest.raises((LinkError, NetlinkError)),
        ):
            pair.create()
        for ns, expected in zip(namespaces, original, strict=True):
            current = json.loads(ns.exec(["ip", "-j", "link", "show"], capture_output=True).stdout)
            assert {link["ifname"]: link["ifindex"] for link in current} == {
                link["ifname"]: link["ifindex"] for link in expected
            }
        with IPRoute() as ipr:
            assert {link["index"] for link in ipr.get_links()} == before
        assert pair.namespace_of("a") is None and pair.namespace_of("b") is None
        for ns in namespaces:
            ns.exec(["ip", "link", "del", "dev", name])
        pair.create()
        _assert_forwarding(pair, namespaces[0])
        pair.destroy()
        assert pair.namespace_of("a") is None and pair.namespace_of("b") is None
    finally:
        for ns in namespaces:
            with contextlib.suppress(Exception):
                ns.destroy()
        with IPRoute() as ipr:
            while owned := [
                link
                for link in ipr.get_links()
                if link.get_attr("IFLA_IFNAME").startswith((prefix, temporary_prefix))
            ]:
                ipr.link("del", index=owned[0]["index"])
