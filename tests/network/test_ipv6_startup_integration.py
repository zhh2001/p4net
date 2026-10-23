"""IPv6 startup readiness and rollback using real namespaces and kernel DAD."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import uuid
from pathlib import Path

import pytest

from p4net import Network
from p4net.network import NetworkError, orchestrator
from p4net.runtime import NetworkNamespace
from p4net.topo import Topology

pytestmark = pytest.mark.integration


def _direct_topology(*, duplicate: bool = False) -> Topology:
    suffix = uuid.uuid4().hex[:6]
    topology = Topology()
    first, second = f"h{suffix}a", f"h{suffix}b"
    topology.add_host(first, ip6="fd00:38::1/64")
    topology.add_host(second, ip6="fd00:38::1/64" if duplicate else "fd00:38::2/64")
    topology.add_link(first, second)
    return topology


def _start(network: Network, entry: str) -> None:
    if entry == "sync":
        network.start()
    else:
        asyncio.run(network.astart())


def _assert_released(topology: Topology) -> None:
    for name in topology.hosts:
        assert not NetworkNamespace(name).exists


@pytest.mark.parametrize("entry", ["sync", "async"])
def test_start_waits_for_configured_ipv6(tmp_path: Path, entry: str) -> None:
    topology = _direct_topology()
    network = Network(topology, log_dir=tmp_path / "logs")
    try:
        _start(network, entry)
        for host in network.hosts.values():
            states = json.loads(
                host.exec(["ip", "-j", "-6", "addr", "show"], capture_output=True).stdout
            )
            for iface, cidr in host.interfaces6.items():
                assert cidr is not None
                address = ipaddress.IPv6Interface(cidr).ip
                matches = [
                    record
                    for item in states
                    if item["ifname"] == iface
                    for record in item.get("addr_info", [])
                    if ipaddress.IPv6Address(record["local"]) == address
                ]
                assert len(matches) == 1
                assert not matches[0].get("tentative", False)
                assert not matches[0].get("dadfailed", False)
        first, second = topology.hosts
        assert network.ping(first, second, count=1, timeout=0.25)
        assert network.ping(second, first, count=1, timeout=0.25)
    finally:
        network.stop()
    _assert_released(topology)


@pytest.mark.parametrize("entry", ["sync", "async"])
def test_kernel_dad_failure_rolls_back(tmp_path: Path, entry: str) -> None:
    topology = _direct_topology(duplicate=True)
    network = Network(topology, unsafe=True, log_dir=tmp_path / "logs")
    try:
        with pytest.raises(NetworkError, match="duplicate address detection") as error:
            _start(network, entry)
        assert "fd00:38::1" in str(error.value)
        assert any(name in str(error.value) for name in topology.hosts)
        assert not network.is_running
    finally:
        network.stop()
    _assert_released(topology)


@pytest.mark.parametrize("entry", ["sync", "async"])
def test_ipv6_timeout_rolls_back(
    tmp_path: Path, entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    topology = _direct_topology()
    execute = NetworkNamespace.exec

    def slow_dad(self: NetworkNamespace, argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["ip", "link", "set"] and argv[-1] == "up" and argv[3] != "lo":
            execute(
                self,
                ["sysctl", "-w", f"net.ipv6.conf.{argv[3]}.dad_transmits=100"],
                capture_output=True,
            )
        return execute(self, argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(NetworkNamespace, "exec", slow_dad)
    monkeypatch.setattr(orchestrator, "_IPV6_READY_TIMEOUT", 0.1, raising=False)
    network = Network(topology, log_dir=tmp_path / "logs")
    try:
        with pytest.raises(NetworkError, match=r"IPv6.*not ready"):
            _start(network, entry)
        assert not network.is_running
    finally:
        network.stop()
    _assert_released(topology)


async def test_cancelled_ipv6_startup_releases_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    topology = _direct_topology()
    execute = NetworkNamespace.exec
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()

    def observe(self: NetworkNamespace, argv: list[str], **kwargs: object) -> object:
        result = execute(self, argv, **kwargs)  # type: ignore[arg-type]
        if argv == ["ip", "-j", "-6", "addr", "show"]:
            loop.call_soon_threadsafe(entered.set)
        return result

    monkeypatch.setattr(NetworkNamespace, "exec", observe)
    network = Network(topology, log_dir=tmp_path / "logs")
    startup = asyncio.create_task(network.astart())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(startup, timeout=5)
        assert not network.is_running
        assert not network.hosts
    finally:
        await asyncio.gather(startup, return_exceptions=True)
        await network.astop()
    _assert_released(topology)
