"""Minimal p4net quick-start: two hosts plus one BMv2 switch.

The bundled `quick_start.p4` is a port-2-port swap (port 1 <-> port 2),
so no runtime table programming is needed for hosts on opposite ports to
reach each other. Run as root so the orchestrator can create namespaces
and veth pairs:

    sudo python examples/quick_start/quick_start.py
"""

from __future__ import annotations

from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent


def main() -> None:
    topo = Topology()
    h1 = topo.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    h2 = topo.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    s1 = topo.add_switch("s1", p4_src=HERE / "quick_start.p4")
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(h2, s1, port_b=2)

    with Network(topo) as net:
        print("hosts:", list(net.hosts))
        print("switches:", list(net.switches))
        # Static ARP so ICMP unicast doesn't have to wait for ARP resolution.
        net.host("h1").exec(
            [
                "ip",
                "neigh",
                "replace",
                "10.0.0.2",
                "lladdr",
                "00:00:00:00:00:02",
                "dev",
                "h1-eth0",
                "nud",
                "permanent",
            ]
        )
        net.host("h2").exec(
            [
                "ip",
                "neigh",
                "replace",
                "10.0.0.1",
                "lladdr",
                "00:00:00:00:00:01",
                "dev",
                "h2-eth0",
                "nud",
                "permanent",
            ]
        )
        print("pingall:", net.pingall())


if __name__ == "__main__":
    main()
