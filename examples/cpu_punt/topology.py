"""One host, one switch, all dataplane traffic punted to CPU.

Run with:

    sudo p4net examples/cpu_punt/topology.py

Then in the shell:

    h1 cmd ping -c 3 -W 1 10.0.0.99    # generates ARP traffic
    s1 packet listen count=3 timeout=5  # observe punted packets

Or send a packet from controller to host 1:

    s1 packet send ffffffffffff000000000001880b48656c6c6f \\
        metadata: egress_port=1
"""

from __future__ import annotations

from pathlib import Path

from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()
h1 = topology.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
s1 = topology.add_switch(
    "s1",
    p4_src=HERE / "cpu_punt.p4",
    cpu_port=510,
)
topology.add_link(h1, s1, port_b=1)


if __name__ == "__main__":
    from p4net.cli.main import main

    raise SystemExit(main([__file__]))
