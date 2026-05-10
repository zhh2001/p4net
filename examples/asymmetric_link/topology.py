"""Two hosts plus one switch with asymmetric link delays.

The h1↔s1 link adds 200 ms egress delay at h1 (h1→s1 direction); the
h2↔s1 link adds 20 ms egress delay at s1 (s1→h2 direction). End-to-end
one-way h1→h2 delay is therefore ~220 ms; reverse h2→h1 is ~0 ms; ping
RTT from h1 to h2 is ~220 ms.

Run with:

    sudo p4net examples/asymmetric_link/topology.py

Then in the shell:

    h1 ping h2 count=10 timeout=2
"""

from __future__ import annotations

from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()
h1 = topology.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topology.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topology.add_switch("s1", p4_src=HERE / "asymmetric.p4")
# h1↔s1: shape only the h1→s1 direction (egress at h1).
topology.add_link(h1, s1, port_b=1, delay_a_to_b="200ms")
# h2↔s1: shape only the s1→h2 direction. With a=h2 and b=s1, "b→a" =
# "s1→h2", so delay_b_to_a applies in that direction.
topology.add_link(h2, s1, port_b=2, delay_b_to_a="20ms")


def setup(net: Network) -> None:
    """Pre-seed static ARP."""
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


if __name__ == "__main__":
    from p4net.cli.main import main

    raise SystemExit(main([__file__]))
