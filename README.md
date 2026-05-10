# p4net

[![CI](https://github.com/zhh2001/p4net/actions/workflows/ci.yml/badge.svg)](https://github.com/zhh2001/p4net/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](#)

A P4Runtime-native SDN simulation framework for BMv2.

Status: 0.1.0 — first public release. APIs are stable enough for lab use; expect refinement before 1.0.

## Features

- P4Runtime-native control plane.
- BMv2 `simple_switch_grpc` data plane.
- Linux network-namespace based hosts.
- veth-based links with `tc`/`netem` impairment.
- Programmable Python topology DSL.
- Interactive CLI.
- Per-port packet capture.
- No OpenFlow, no Open vSwitch, no Docker.

## Requirements

- Linux kernel >= 5.4.
- Python 3.10+.
- BMv2 and p4c installed system-wide.
- Root or `CAP_NET_ADMIN` to manage namespaces and veth devices.

## Installation

From a fresh checkout:

```
git clone https://github.com/zhh2001/p4net
cd p4net
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

PyPI distribution may follow a future release.

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

A complete runnable version, including the matching `quick_start.p4`
(a port-2-port swap that needs no runtime table programming) and a tiny
static-ARP setup, lives in `examples/quick_start/`. Run it with:

```
sudo python examples/quick_start/quick_start.py
```

or, equivalently, with the `p4net` console script installed by
`pip install -e .`:

```
sudo p4net examples/quick_start/quick_start.py
```

The console script loads any `.py` file that defines a module-level
`topology: Topology` (and optionally `setup(net)`), brings up the
network, and drops you into an interactive shell. If `sudo` strips your
venv from `PATH`, run the binary explicitly: `sudo env "PATH=$PATH" p4net ...`.

## Examples

- [`examples/quick_start/`](examples/quick_start/) — minimal two-host
  network using a hardcoded port-swap pipeline.
- [`examples/l3_forwarding/`](examples/l3_forwarding/) — two hosts with
  runtime-programmed `ipv4_lpm` forwarding and pre-seeded static ARP.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — module layout and
  design decisions.
- [`docs/tutorial.md`](docs/tutorial.md) — walkthrough from a single
  host up to a programmed two-host network.
- [`docs/cli.md`](docs/cli.md) — CLI reference for the `p4net` shell.
- [Roadmap](docs/roadmap.md)

## License

Apache-2.0.
