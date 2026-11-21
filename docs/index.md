---
description: P4Net is a Python framework for BMv2 and P4Runtime experiments on Linux.
---

# P4Net

P4Net is a Python framework for BMv2 and P4Runtime experiments on Linux. Use it for P4 teaching, controller prototyping, and forwarding tests.

P4Net connects nodes with virtual Ethernet (`veth`) pairs. Each host gets its own Linux network namespace. BMv2 software switches run in the same namespace as the `p4net` process, normally the system's root network namespace.

P4Net controls BMv2 switches through P4Runtime, a protocol also supported by compatible hardware switches. Throughput and latency measured in P4Net reflect the software forwarding and Linux environment of the experiment. See [Known Limitations](known-limitations.md).

## Install

Follow the [installation guide](getting-started/installation.md) to set up p4c, BMv2, and the `p4net` CLI.

## Try L3 forwarding

With the CLI installed, clone the repository and launch the example.

```sh
git clone https://github.com/zhh2001/p4net.git
cd p4net
sudo p4net examples/l3_forwarding/topology.py
```

At the `p4net>` prompt, inspect the switch's IPv4 routes and then test connectivity between the hosts.

```text
s1 table dump MyIngress.ipv4_lpm
pingall
```

The table shows two `/32` routes, and `pingall` reports `2/2 succeeded`. Enter `exit` to stop the topology. See the [L3 forwarding example](examples/l3-forwarding.md) for the P4 program and startup configuration.

## Why P4Net

- **Start BMv2 with P4Runtime in one command.** P4Net compiles the P4 programs with `p4c`, starts BMv2, waits for the switches to be ready, connects its built-in P4Runtime client, and installs the pipelines.
- **Reuse the topology in automated tests.** The CLI runs the optional `setup(net)` function before opening the shell. A Python test can use the same topology and setup function, with `Network` as a context manager for startup and cleanup.
- **Debug hosts and switches in one shell.** Run host commands and ping tests alongside P4Runtime table updates, counter reads, and PacketIn and PacketOut operations. See the [CLI reference](cli.md).
- **Configure several switches concurrently.** The async P4Runtime example installs nine forwarding entries on three BMv2 switches from one Python event loop. See the [async example](examples/async-concurrent.md).
- **Reproduce link conditions.** Configure IPv4 and IPv6 hosts, set bandwidth, delay, jitter, or packet loss for each link direction, and capture traffic on switch ports.
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

- [Quick Start](getting-started/quickstart.md) builds a two-host topology from scratch.
- [Tutorial](tutorial.md) walks through dual-stack forwarding, table programming, directional link delay, and CPU-port packet I/O.
- [Examples](examples/index.md) contains nine runnable topologies, including in-band telemetry and concurrent switch configuration.
- [Architecture](architecture.md) shows how the Python packages fit together.
- [API Reference](api/network.md) documents the Python interfaces.

## Project status

P4Net **v1.10.1** is a stable release. See the [changelog](changelog.md), [API stability policy](api-stability.md), and [roadmap](roadmap.md).
