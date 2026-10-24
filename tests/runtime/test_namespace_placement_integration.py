"""Create same-named veth endpoints directly in separate namespaces."""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path

import pytest
from pyroute2 import IPRoute
from pyroute2.netlink.exceptions import NetlinkError

from p4net import Network
from p4net.runtime import LinkError, NetworkNamespace, VethPair
from p4net.topo import Topology

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("alphabet", ["ascii", "utf8"])
def test_same_name_in_root_and_two_host_namespaces(alphabet: str) -> None:
    prefix = uuid.uuid4().hex[:6]
    shared = prefix + ("shared" if alphabet == "ascii" else "a界界00")
    namespaces = [NetworkNamespace("ns" + uuid.uuid4().hex[:8]) for _ in range(2)]
    holder = VethPair(shared, prefix + "peer")
    pair = None
    try:
        for ns in namespaces:
            ns.create()
            ns.exec(["ip", "link", "set", "lo", "up"])
        holder.create()
        with IPRoute() as ipr:
            root_index = ipr.link_lookup(ifname=shared)
        pair = VethPair(shared, shared, namespace_a=namespaces[0], namespace_b=namespaces[1])
        pair.create()
        for side, address in (("a", "10.234.0.1/24"), ("b", "10.234.0.2/24")):
            pair.set_address(side, address)  # type: ignore[arg-type]
            pair.set_up(side)  # type: ignore[arg-type]
        reply = namespaces[0].exec(["ping", "-c", "1", "-W", "2", "10.234.0.2"])
        assert reply.returncode == 0
        pair.destroy()
        for ns in namespaces:
            result = ns.exec(["ip", "-o", "link", "show"], capture_output=True)
            assert shared.encode() not in result.stdout
        with IPRoute() as ipr:
            assert ipr.link_lookup(ifname=shared) == root_index
    finally:
        for ns in namespaces:
            with contextlib.suppress(Exception):
                ns.destroy()
        with contextlib.suppress(Exception):
            holder.destroy()
        with IPRoute() as ipr:
            while owned := [
                link for link in ipr.get_links() if link.get_attr("IFLA_IFNAME").startswith(prefix)
            ]:
                ipr.link("del", index=owned[0]["index"])


@pytest.mark.parametrize("failed_side", [0, 1])
def test_failed_placement_preserves_existing_interface(failed_side: int) -> None:
    prefix = uuid.uuid4().hex[:6]
    shared = prefix + "shared"
    namespaces = [NetworkNamespace("ns" + uuid.uuid4().hex[:8]) for _ in range(2)]
    try:
        for ns in namespaces:
            ns.create()
        namespaces[failed_side].exec(["ip", "link", "add", "name", shared, "type", "dummy"])
        with IPRoute() as ipr:
            before = {link["index"] for link in ipr.get_links()}
        pair = VethPair(shared, shared, namespace_a=namespaces[0], namespace_b=namespaces[1])
        with pytest.raises((LinkError, NetlinkError)):
            pair.create()
        with IPRoute() as ipr:
            assert {link["index"] for link in ipr.get_links()} == before
        other = namespaces[1 - failed_side].exec(["ip", "-o", "link", "show"], capture_output=True)
        assert shared.encode() not in other.stdout
        namespaces[failed_side].exec(["ip", "link", "show", "dev", shared])
        assert pair.namespace_of("a") is None and pair.namespace_of("b") is None
    finally:
        for ns in namespaces:
            with contextlib.suppress(Exception):
                ns.destroy()


@pytest.mark.parametrize("alphabet", ["ascii", "utf8"])
def test_network_same_name_at_both_host_endpoints(tmp_path: Path, alphabet: str) -> None:
    suffix = uuid.uuid4().hex[:6]
    first, second = f"h{suffix}a", f"h{suffix}b"
    shared = suffix + ("shared" if alphabet == "ascii" else "a界界00")
    topo = Topology()
    topo.add_host(first, ip="10.233.0.1/24")
    topo.add_host(second, ip="10.233.0.2/24")
    topo.add_link(first, second)
    data = topo.to_dict()
    for endpoint in (data["links"][0]["a"], data["links"][0]["b"]):
        endpoint["iface_name"] = shared
    restored = Topology.from_dict(data)
    restored.validate()
    with Network(restored, log_dir=tmp_path / "logs") as network:
        assert set(network.host(first).interfaces) == {shared}
        assert set(network.host(second).interfaces) == {shared}
        assert network.ping(first, second, count=1, timeout=0.1)
        assert network.ping(second, first, count=1, timeout=0.1)
