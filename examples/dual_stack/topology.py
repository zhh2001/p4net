"""Two hosts plus one switch carrying both IPv4 and IPv6.

The pipeline is L3-agnostic (it just swaps ports 1 and 2), so both v4 and
v6 traverse identically. ``setup(net)`` seeds static ARP and ND so the
hosts don't have to resolve neighbours at run time.

Run with:

    sudo p4net examples/dual_stack/topology.py

Then in the shell:

    pingall
    h1 ping h2
    h1 ping6 h2
"""

from __future__ import annotations

from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()
h1 = topology.add_host(
    "h1",
    ip="10.0.0.1/24",
    mac="00:00:00:00:00:01",
    ip6="fd00::1/64",
)
h2 = topology.add_host(
    "h2",
    ip="10.0.0.2/24",
    mac="00:00:00:00:00:02",
    ip6="fd00::2/64",
)
s1 = topology.add_switch("s1", p4_src=HERE / "dual_stack.p4")
topology.add_link(h1, s1, port_b=1)
topology.add_link(h2, s1, port_b=2)


def setup(net: Network) -> None:
    """Pre-seed static ARP and ND so ICMP unicast doesn't have to resolve."""
    h1 = net.host("h1")
    h2 = net.host("h2")
    h1.exec(
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
    h2.exec(
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
    h1.exec(
        [
            "ip",
            "-6",
            "neigh",
            "replace",
            "fd00::2",
            "lladdr",
            "00:00:00:00:00:02",
            "dev",
            "h1-eth0",
            "nud",
            "permanent",
        ]
    )
    h2.exec(
        [
            "ip",
            "-6",
            "neigh",
            "replace",
            "fd00::1",
            "lladdr",
            "00:00:00:00:00:01",
            "dev",
            "h2-eth0",
            "nud",
            "permanent",
        ]
    )


if __name__ == "__main__":
    from p4net.cli.main import main

    raise SystemExit(main([__file__]))
