"""Minimal p4net quick-start: two hosts plus one BMv2 switch.

The bundled `quick_start.p4` is a port-2-port swap (port 1 <-> port 2),
so no runtime table programming is needed for hosts on opposite ports
to reach each other. Run as root so the orchestrator can create
namespaces and veth pairs:

    sudo python examples/quick_start/quick_start.py
    sudo p4net examples/quick_start/quick_start.py

The first form is a self-contained script. The second uses the `p4net`
console script (installed by `pip install -e .`) to load this file as a
topology module: `topology` and `setup(net)` are the two named hooks
that the console script looks for.
"""

from __future__ import annotations

from pathlib import Path

from p4net import Network
from p4net.network import RunningHost
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent


def _build_topology() -> Topology:
    topo = Topology()
    h1 = topo.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    h2 = topo.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    s1 = topo.add_switch("s1", p4_src=HERE / "quick_start.p4")
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(h2, s1, port_b=2)
    return topo


# Module-level `topology` for the `p4net` console script.
topology = _build_topology()


def setup(net: Network) -> None:
    """Pre-seed static ARP for both hosts; called by the console script
    after Network.start() and before the shell.

    The same logic also runs inside this script's `__main__` block.
    """
    _add_static_arp(net.host("h1"), "10.0.0.2", "00:00:00:00:00:02")
    _add_static_arp(net.host("h2"), "10.0.0.1", "00:00:00:00:00:01")


def _add_static_arp(host: RunningHost, target_ip: str, target_mac: str) -> None:
    iface = next(iter(host.interfaces))
    host.exec(
        [
            "ip",
            "neigh",
            "replace",
            target_ip,
            "lladdr",
            target_mac,
            "dev",
            iface,
            "nud",
            "permanent",
        ]
    )


def main() -> None:
    """Same flow as `p4net <this file>` but self-contained for direct invocation."""
    with Network(topology) as net:
        setup(net)
        print("hosts:", list(net.hosts))
        print("switches:", list(net.switches))
        print("pingall:", net.pingall())


if __name__ == "__main__":
    main()
