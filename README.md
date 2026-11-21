# P4Net

[![CI](https://github.com/zhh2001/p4net/actions/workflows/ci.yml/badge.svg)](https://github.com/zhh2001/p4net/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/p4net.svg)](https://pypi.org/project/p4net/)
[![Python Versions](https://img.shields.io/pypi/pyversions/p4net.svg)](https://pypi.org/project/p4net/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/zhh2001/p4net/blob/main/LICENSE)
[![Documentation](https://img.shields.io/badge/docs-zhh2001.github.io%2Fp4net-blue)](https://zhh2001.github.io/p4net/)
[![Status](https://img.shields.io/badge/status-stable-green.svg)](https://zhh2001.github.io/p4net/api-stability/)

P4Net is a Python framework for BMv2 and P4Runtime experiments on Linux.

Status: 1.10.1, stable. Public APIs follow the [API stability rules](https://zhh2001.github.io/p4net/api-stability/). See the [changelog](https://zhh2001.github.io/p4net/changelog/) for new features, fixes and upgrade notes.

## Why use P4Net

- **Start BMv2 with P4Runtime in one command.** P4Net compiles the P4 programs, builds the Linux topology, starts BMv2, connects the built-in P4Runtime client, and installs the pipelines.
- **Reuse one topology for debugging and automated tests.** Define hosts, links, and switches in Python. Put initial table entries in `setup(net)` and reuse that function with the same topology in a Python test.
- **Debug hosts and switches in one shell.** Check connectivity, run host commands, and use P4Runtime to inspect or program tables, read counters, and handle PacketIn and PacketOut. See the [L3 forwarding example](https://zhh2001.github.io/p4net/examples/l3-forwarding/).
- **Configure several switches concurrently.** Use `AsyncP4RuntimeClient` to send P4Runtime requests from one Python event loop. The [async example](https://zhh2001.github.io/p4net/examples/async-concurrent/) installs forwarding entries on a three-switch mesh.
- **Repeat experiments under defined link conditions.** Configure IPv4 and IPv6 hosts, set bandwidth, delay, jitter, and loss per direction, and capture traffic on switch ports.

## Requirements

- Linux kernel >= 5.4.
- Python 3.10+.
- BMv2 and p4c installed system-wide.
- Root or `CAP_NET_ADMIN` to manage namespaces and veth devices.

## Installation

Follow the [installation guide](https://zhh2001.github.io/p4net/getting-started/installation/) to set up p4c, BMv2 and the p4net CLI. For an editable checkout and development tools, see [Contributing](https://zhh2001.github.io/p4net/dev/contributing/).

## Quick Start

```python
from pathlib import Path
from p4net import Network
from p4net.topo import Topology

topo = Topology()
h1 = topo.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topo.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topo.add_switch("s1", p4_src=Path("quick_start.p4"))
topo.add_link(h1, s1, port_b=1)
topo.add_link(h2, s1, port_b=2)

with Network(topo) as net:
    print(net.pingall())
```

A complete runnable version, including the matching `quick_start.p4` (a port-2-port swap that needs no runtime table programming) and a tiny static-ARP setup, lives in `examples/quick_start/`. After installing the CLI, clone the repository and run the example to open the interactive shell.

```sh
git clone https://github.com/zhh2001/p4net.git
cd p4net
sudo p4net examples/quick_start/quick_start.py
```

The console script loads a Python file that defines a module-level `topology: Topology` and optionally `setup(net)`. The [example README](https://github.com/zhh2001/p4net/blob/main/examples/quick_start/README.md#as-a-self-contained-script) also shows how to run the file directly with Python.

## Examples

- [`examples/quick_start/`](https://github.com/zhh2001/p4net/tree/main/examples/quick_start) — minimal two-host network with a hardcoded port-swap pipeline.
- [`examples/l3_forwarding/`](https://github.com/zhh2001/p4net/tree/main/examples/l3_forwarding) — runtime-programmed IPv4 LPM with static ARP.
- [`examples/cpu_punt/`](https://github.com/zhh2001/p4net/tree/main/examples/cpu_punt) — every dataplane packet punted to the controller; demonstrates `<switch> packet send` / `<switch> packet listen`.
- [`examples/dual_stack/`](https://github.com/zhh2001/p4net/tree/main/examples/dual_stack) — two hosts carrying both IPv4 and IPv6.
- [`examples/asymmetric_link/`](https://github.com/zhh2001/p4net/tree/main/examples/asymmetric_link) — per-direction `delay` shaping.
- [`examples/ipv6_lpm/`](https://github.com/zhh2001/p4net/tree/main/examples/ipv6_lpm) — runtime-programmed IPv6 LPM forwarding.
- [`examples/int/`](https://github.com/zhh2001/p4net/tree/main/examples/int) — single-switch in-band network telemetry: the pipeline inserts a 14-byte INT shim into every forwarded packet; a raw-socket listener decodes it on the receiver.
- [`examples/int_multi_hop/`](https://github.com/zhh2001/p4net/tree/main/examples/int_multi_hop) — multi-hop INT on a two-switch linear path; the listener decodes the full hop-by-hop stack. Production-style telemetry topology.
- [`examples/async_concurrent/`](https://github.com/zhh2001/p4net/tree/main/examples/async_concurrent) — three-switch mesh with IPv4 forwarding entries installed concurrently using `AsyncP4RuntimeClient`.

## Documentation

[English documentation](https://zhh2001.github.io/p4net/) | [中文文档](https://zhh2001.github.io/p4net/zh/)

Both editions cover installation, tutorials, the CLI, and runnable examples.

The source for these pages lives under [`docs/`](https://github.com/zhh2001/p4net/tree/main/docs).

## License

Apache-2.0.
