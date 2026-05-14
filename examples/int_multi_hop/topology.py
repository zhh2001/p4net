"""Linear 4-node topology demonstrating multi-hop INT.

    h1 (10.0.0.1/24) --- s1 --------- s2 --- h2 (10.0.0.2/24)
                    port1  port2 port1  port2

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

import json
import os
from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

# Coordination file consumed by ``listener.py`` so the listener can align
# each switch's per-process BMv2 timestamp to wall-clock microseconds:
#
#     wall_clock_us = switch.boot_timestamp_us + shim.ingress_timestamp_us
#
# Written at the end of ``setup(net)`` once both switches are running.
# The path is overridable via the ``P4NET_INT_BOOT_TIMES_PATH`` environment
# variable so multiple multi-hop INT topologies can coexist on one host.
# Pass it with ``sudo -E`` to preserve the variable across privilege
# escalation; both topology.py and listener.py read the same env var.
BOOT_TIMES_PATH = Path(
    os.environ.get(
        "P4NET_INT_BOOT_TIMES_PATH",
        "/tmp/p4net-int-multi-hop-boot-times.json",
    )
)

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

    # Publish each switch's BMv2 boot timestamp so the listener can align
    # per-switch ``ingress_timestamp_us`` values to a common wall clock.
    # ``Network.boot_timestamps`` (v1.5+) returns the same mapping as the
    # previous manual ``{name: net.switch(name).boot_timestamp_us}`` form,
    # and adapts automatically if more switches are added later.
    boot_times = net.boot_timestamps
    BOOT_TIMES_PATH.write_text(json.dumps(boot_times, indent=2))
    print(f"boot timestamps written to {BOOT_TIMES_PATH}", flush=True)


if __name__ == "__main__":
    from p4net.cli.main import main

    raise SystemExit(main([__file__]))
