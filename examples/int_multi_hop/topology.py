"""Linear 4-node topology demonstrating multi-hop INT.

    h1 (10.0.0.1/24) --- s1 --- s2 --- h2 (10.0.0.2/24)
                     port1    port2 port1   port2

Both switches run the same P4 program (``int_multi_hop.p4``). Each switch's
``switch_id`` register is written at start-up via the v1.2 register API:
s1 gets ``1``, s2 gets ``2``. L2 forwarding is exact-match on destination
MAC; static ARP is seeded between the hosts.

Run as root:

    sudo p4net examples/int_multi_hop/topology.py

Then in another terminal:

    sudo python3 examples/int_multi_hop/listener.py

And from the p4net shell (or a third terminal):

    sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2

The listener prints one block per packet, with one line per traversed
switch (two lines per packet in this topology).
"""

from __future__ import annotations

from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()
h1 = topology.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topology.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topology.add_switch("s1", p4_src=HERE / "int_multi_hop.p4")
s2 = topology.add_switch("s2", p4_src=HERE / "int_multi_hop.p4")

topology.add_link(h1, s1, port_b=1)
topology.add_link(s1, s2, port_a=2, port_b=1)
topology.add_link(s2, h2, port_a=2)


def setup(net: Network) -> None:
    """Static ARP, l2_forward tables, switch_id registers."""
    h1_rt = net.host("h1")
    h2_rt = net.host("h2")
    s1_rt = net.switch("s1")
    s2_rt = net.switch("s2")

    h1_rt.exec(
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
    h2_rt.exec(
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

    # Per-switch INT identity.
    s1_rt.client.write_register("MyIngress.switch_id_reg", index=0, value=1)
    s2_rt.client.write_register("MyIngress.switch_id_reg", index=0, value=2)

    # L2 forwarding: route by destination MAC out the link toward the host.
    for sw_rt in (s1_rt, s2_rt):
        sw_rt.client.insert_table_entry(
            table="MyIngress.l2_forward",
            match={"hdr.ethernet.dstAddr": "00:00:00:00:00:02"},
            action="MyIngress.set_egress_port",
            params={"port": 2},
        )
        sw_rt.client.insert_table_entry(
            table="MyIngress.l2_forward",
            match={"hdr.ethernet.dstAddr": "00:00:00:00:00:01"},
            action="MyIngress.set_egress_port",
            params={"port": 1},
        )


if __name__ == "__main__":
    from p4net.cli.main import main

    raise SystemExit(main([__file__]))
