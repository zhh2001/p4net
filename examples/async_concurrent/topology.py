"""Three-switch full-mesh topology demonstrating the v1.6 async client.

Topology::

    h1 (10.0.1.1/24)            h2 (10.0.2.1/24)            h3 (10.0.3.1/24)
        |                           |                           |
        | port 1                    | port 1                    | port 1
       s1 -------- port 2 ---------- s2 -------- port 3 -------- s3
        |                                                       |
        |                  port 3                       port 2  |
        +-------------------------------------------------------+

Three hosts each on their own /24, on different switches. Switches are
fully meshed (s1-s2, s2-s3, s3-s1). Each switch loads the same P4 binary
(``concurrent.p4``) and gets a per-switch ipv4_lpm table populated from
Python via the v1.6 ``AsyncP4RuntimeClient``.

``setup(net)`` drives all six concurrent inserts (2 cross-subnet routes
per switch) via ``asyncio.gather`` against three switches' async clients
in parallel — visibly faster than the sequential equivalent on a
multi-switch topology.

Run as root:

    sudo p4net examples/async_concurrent/topology.py

Then in the shell::

    pingall
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()

h1 = topology.add_host("h1", ip="10.0.1.1/24", mac="00:00:00:00:00:01", default_route="10.0.1.254")
h2 = topology.add_host("h2", ip="10.0.2.1/24", mac="00:00:00:00:00:02", default_route="10.0.2.254")
h3 = topology.add_host("h3", ip="10.0.3.1/24", mac="00:00:00:00:00:03", default_route="10.0.3.254")

s1 = topology.add_switch("s1", p4_src=HERE / "concurrent.p4")
s2 = topology.add_switch("s2", p4_src=HERE / "concurrent.p4")
s3 = topology.add_switch("s3", p4_src=HERE / "concurrent.p4")

# Each switch to its host on port 1
topology.add_link(h1, s1, port_b=1)
topology.add_link(h2, s2, port_b=1)
topology.add_link(h3, s3, port_b=1)
# Switch mesh: s1.port2 ↔ s2.port2, s2.port3 ↔ s3.port2, s3.port3 ↔ s1.port3.
topology.add_link(s1, s2, port_a=2, port_b=2)
topology.add_link(s2, s3, port_a=3, port_b=2)
topology.add_link(s3, s1, port_a=3, port_b=3)


# Per-switch routing table: each switch points at port 1 for its own
# subnet, and routes the other two subnets out the mesh ports above.
_ROUTES: dict[str, list[tuple[str, int]]] = {
    "s1": [("10.0.1.0/24", 1), ("10.0.2.0/24", 2), ("10.0.3.0/24", 3)],
    "s2": [("10.0.2.0/24", 1), ("10.0.1.0/24", 2), ("10.0.3.0/24", 3)],
    "s3": [("10.0.3.0/24", 1), ("10.0.1.0/24", 3), ("10.0.2.0/24", 2)],
}


async def _async_setup(net: Network) -> dict[str, Any]:
    """Concurrent table programming across all three switches.

    Returns a dict with timing measurements so the example can be
    instrumented from the shell or a test harness.
    """
    switches = [net.switch(n) for n in ("s1", "s2", "s3")]
    clients = [sw.async_client for sw in switches]

    # Connect all clients concurrently.
    await asyncio.gather(*(c.connect() for c in clients))

    # Build the full set of insert coroutines, then gather them.
    tasks = []
    for sw, client in zip(switches, clients, strict=True):
        for cidr, port in _ROUTES[sw.name]:
            tasks.append(
                client.insert_table_entry(
                    "MyIngress.ipv4_lpm",
                    {"hdr.ipv4.dstAddr": cidr},
                    "MyIngress.set_egress_port",
                    {"port": port},
                )
            )
    import time as _time

    t0 = _time.perf_counter()
    await asyncio.gather(*tasks)
    t1 = _time.perf_counter()
    concurrent_ms = (t1 - t0) * 1000.0

    # Disconnect (Network.stop would do this anyway).
    await asyncio.gather(*(c.disconnect() for c in clients))
    return {"concurrent_ms": concurrent_ms, "inserts": len(tasks)}


def setup(net: Network) -> None:
    """Static ARP across subnets + concurrent table programming."""
    # Each host learns the cross-subnet gateway MACs. We use a single
    # virtual gateway MAC per subnet (one MAC == one switch's port-1).
    # On the wire the switches forward by destination IP/24, so the
    # source MAC isn't relevant to the LPM table.
    arp_entries = [
        ("h1", "10.0.2.1", "00:00:00:00:00:02"),
        ("h1", "10.0.3.1", "00:00:00:00:00:03"),
        ("h2", "10.0.1.1", "00:00:00:00:00:01"),
        ("h2", "10.0.3.1", "00:00:00:00:00:03"),
        ("h3", "10.0.1.1", "00:00:00:00:00:01"),
        ("h3", "10.0.2.1", "00:00:00:00:00:02"),
    ]
    for host, ip, mac in arp_entries:
        net.host(host).exec(
            [
                "ip",
                "neigh",
                "replace",
                ip,
                "lladdr",
                mac,
                "dev",
                f"{host}-eth0",
                "nud",
                "permanent",
            ]
        )

    timings = asyncio.run(_async_setup(net))
    print(
        f"async setup: {timings['inserts']} table entries installed "
        f"concurrently in {timings['concurrent_ms']:.2f} ms",
        flush=True,
    )


if __name__ == "__main__":
    from p4net.cli.main import main

    raise SystemExit(main([__file__]))
