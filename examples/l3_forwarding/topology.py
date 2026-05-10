"""Two hosts on a /24, ipv4_lpm forwarding programmed via P4Runtime.

The P4 program (`ipv4_lpm.p4`) defines a single LPM table that matches the
destination IPv4 address and sets an egress port. Forwarding entries are
installed at runtime by `setup(net)`, which also pre-seeds static ARP so
ICMP unicast does not have to resolve neighbours at test time.

Run as root:

    sudo python examples/l3_forwarding/topology.py
    sudo p4net examples/l3_forwarding/topology.py
"""

from __future__ import annotations

from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()
h1 = topology.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topology.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topology.add_switch("s1", p4_src=HERE / "ipv4_lpm.p4")
topology.add_link(h1, s1, port_b=1)
topology.add_link(h2, s1, port_b=2)


def setup(net: Network) -> None:
    """Pre-seed static ARP and install ipv4_lpm forwarding entries."""
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

    s1 = net.switch("s1")
    s1.client.insert_table_entry(
        table="MyIngress.ipv4_lpm",
        match={"hdr.ipv4.dstAddr": "10.0.0.1/32"},
        action="MyIngress.set_egress_port",
        params={"port": 1},
    )
    s1.client.insert_table_entry(
        table="MyIngress.ipv4_lpm",
        match={"hdr.ipv4.dstAddr": "10.0.0.2/32"},
        action="MyIngress.set_egress_port",
        params={"port": 2},
    )


if __name__ == "__main__":
    from p4net.cli.main import main

    raise SystemExit(main([__file__]))
