"""Two IPv6 hosts forwarded by an `ipv6_lpm` table programmed at runtime.

Run with:

    sudo p4net examples/ipv6_lpm/topology.py

Then in the shell:

    pingall6
    h1 ping6 h2
    s1 table dump MyIngress.ipv6_lpm
    s1 counter MyIngress.ipv6_pkts
"""

from __future__ import annotations

from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()
h1 = topology.add_host("h1", ip6="fd00::1/64", mac="00:00:00:00:00:01")
h2 = topology.add_host("h2", ip6="fd00::2/64", mac="00:00:00:00:00:02")
s1 = topology.add_switch("s1", p4_src=HERE / "ipv6_lpm.p4")
topology.add_link(h1, s1, port_b=1)
topology.add_link(h2, s1, port_b=2)


def setup(net: Network) -> None:
    """Seed static ND and install ipv6_lpm forwarding entries."""
    h1 = net.host("h1")
    h2 = net.host("h2")
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
    s1 = net.switch("s1")
    s1.client.insert_table_entry(
        table="MyIngress.ipv6_lpm",
        match={"hdr.ipv6.dstAddr": "fd00::1/128"},
        action="MyIngress.set_egress_port",
        params={"port": 1},
    )
    s1.client.insert_table_entry(
        table="MyIngress.ipv6_lpm",
        match={"hdr.ipv6.dstAddr": "fd00::2/128"},
        action="MyIngress.set_egress_port",
        params={"port": 2},
    )


if __name__ == "__main__":
    from p4net.cli.main import main

    raise SystemExit(main([__file__]))
