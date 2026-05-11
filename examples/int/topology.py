"""Two hosts, one switch, IPv4 forwarding with INT shim insertion.

The P4 program (`int.p4`) inserts a 14-byte INT shim header between the
Ethernet and IPv4 headers on every forwarded packet. The shim carries the
switch identifier, ingress timestamp, egress port, queue depth, and the
original etherType (so a receiver can recover the inner IPv4 header).

Run as root:

    sudo p4net examples/int/topology.py

Then in a separate terminal on h2 (e.g. ``h2 xterm`` from the CLI):

    sudo python3 /path/to/examples/int/listener.py --iface h2-eth0

And from another terminal:

    sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2

The listener prints one structured line per INT-stamped frame.
"""

from __future__ import annotations

from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()
h1 = topology.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topology.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topology.add_switch("s1", p4_src=HERE / "int.p4")
topology.add_link(h1, s1, port_b=1)
topology.add_link(h2, s1, port_b=2)


def setup(net: Network) -> None:
    """Static ARP both sides; LPM entries; write the switch_id register."""
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

    # Assign this switch's INT identifier. The INT shim stamps every
    # forwarded packet with this value. For multi-switch topologies,
    # give each switch a distinct id.
    s1.client.write_register("MyIngress.switch_id", index=0, value=1)


if __name__ == "__main__":
    from p4net.cli.main import main

    raise SystemExit(main([__file__]))
