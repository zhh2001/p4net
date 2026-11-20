---
description: Run P4 network experiments on Linux with Python, BMv2 software switches, and P4Runtime.
---

# p4net

p4net is a Python framework for running P4 network experiments on Linux. Use it for teaching labs, P4Runtime controller prototypes, and tests of P4 forwarding behavior.

p4net compiles P4 programs with `p4c` and connects nodes with virtual Ethernet (`veth`) pairs. Each host gets its own Linux network namespace. BMv2 software switches run in the same namespace as the p4net process, normally the system's root network namespace.

p4net controls BMv2 switches through P4Runtime, a protocol also supported by compatible hardware switches. Throughput and latency measured in p4net reflect the software forwarding and Linux environment of the experiment. See [Known Limitations](known-limitations.md).

## Install

Follow the [installation guide](getting-started/installation.md) to set up p4c, BMv2, and the p4net CLI. Then run your first topology in [Quick Start](getting-started/quickstart.md).

## What you can do

- **Build topologies in Python.** Connect IPv4 and IPv6 hosts to switches. Set bandwidth, delay, jitter, or packet loss separately for each link direction.
- **Program switches.** Use the sync or async P4Runtime client to install table entries, read indirect counters, manage multicast groups, receive PacketIn, and send PacketOut.
- **Inspect a running network.** Use the [interactive CLI](cli.md) to run host commands, check connectivity, and inspect switch tables.
- **Export topology diagrams.** Save the topology as DOT or use Graphviz to render it as SVG, PNG, or PDF.

## Python example

[Install p4net in a virtual environment](getting-started/installation.md#project-environments). In the project directory, save the [P4 program from Quick Start](getting-started/quickstart.md#create-two-files) as `port_swap.p4` and the script below as `demo.py`.

```python
from pathlib import Path

from p4net import Network
from p4net.topo import Topology

topo = Topology()
h1 = topo.add_host("h1", ip="10.0.0.1/24")
h2 = topo.add_host("h2", ip="10.0.0.2/24")
s1 = topo.add_switch("s1", p4_src=Path(__file__).with_name("port_swap.p4"))
topo.add_link(h1, s1, port_b=1)
topo.add_link(h2, s1, port_b=2)

with Network(topo) as net:
    print(net.ping("h1", "h2"))
```

Run the script from the project directory with the virtual environment's Python.

```sh
sudo .venv/bin/python demo.py
```

The script prints `True` when `h1` receives a reply from `h2`. The network shuts down when the `with` block ends.

## Guides and reference

- [Tutorial](tutorial.md) walks through dual-stack forwarding, table programming, directional link delay, and CPU-port packet I/O.
- [Examples](examples/index.md) contains nine runnable topologies, including in-band telemetry and concurrent switch configuration.
- [Architecture](architecture.md) shows how the Python packages fit together.
- [API Reference](api/network.md) documents the Python interfaces.

## Project status

p4net **v1.10.0** is a stable release. See the [changelog](changelog.md), [API stability policy](api-stability.md), and [roadmap](roadmap.md).
