# p4net

[![CI](https://github.com/zhh2001/p4net/actions/workflows/ci.yml/badge.svg)](https://github.com/zhh2001/p4net/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/p4net.svg)](https://pypi.org/project/p4net/)
[![Python Versions](https://img.shields.io/pypi/pyversions/p4net.svg)](https://pypi.org/project/p4net/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Documentation](https://img.shields.io/badge/docs-zhh2001.github.io%2Fp4net-blue)](https://zhh2001.github.io/p4net/)
[![Status](https://img.shields.io/badge/status-stable-green.svg)](https://zhh2001.github.io/p4net/api-stability/)

A P4Runtime-native SDN simulation framework for BMv2.

Status: 1.4.0 — stable. Public API committed per [API Stability](https://zhh2001.github.io/p4net/api-stability/). Patch releases for fixes; minor releases for new functionality with deprecation cycles where needed.

## Features

- P4Runtime-native control plane.
- BMv2 `simple_switch_grpc` data plane.
- Linux network-namespace based hosts.
- veth-based links with `tc`/`netem` impairment.
- Programmable Python topology DSL.
- Interactive CLI.
- Per-port packet capture.
- P4Runtime CPU-port packet I/O (`<switch> packet send` / `listen`).
- IPv4 and IPv6 host addressing with per-interface sysctl gating.
- Asymmetric link impairment (per-direction `bandwidth`, `delay`, `jitter`, `loss_pct`).
- Topology visualization (Graphviz DOT / PNG / SVG).
- In-band network telemetry (INT) — single-switch introduction plus a multi-hop production-style example with wall-clock-aligned cross-switch timestamps via `RunningSwitch.boot_timestamp_us`.
- Direct P4 register read/write via P4Runtime gRPC.
- Unified `p4net.*` logger hierarchy with CLI verbosity control.
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
  network with a hardcoded port-swap pipeline.
- [`examples/l3_forwarding/`](examples/l3_forwarding/) —
  runtime-programmed IPv4 LPM with static ARP.
- [`examples/cpu_punt/`](examples/cpu_punt/) — every dataplane packet
  punted to the controller; demonstrates `<switch> packet send` /
  `<switch> packet listen`.
- [`examples/dual_stack/`](examples/dual_stack/) — two hosts carrying
  both IPv4 and IPv6.
- [`examples/asymmetric_link/`](examples/asymmetric_link/) —
  per-direction `delay` shaping.
- [`examples/ipv6_lpm/`](examples/ipv6_lpm/) — runtime-programmed IPv6
  LPM forwarding.
- [`examples/int/`](examples/int/) — single-switch in-band network
  telemetry: the pipeline inserts a 14-byte INT shim into every
  forwarded packet; a raw-socket listener decodes it on the receiver.
- [`examples/int_multi_hop/`](examples/int_multi_hop/) — multi-hop INT
  on a two-switch linear path; the listener decodes the full hop-by-hop
  stack. Production-style telemetry topology.

## Documentation

**Full documentation: <https://zhh2001.github.io/p4net/>**

The English site is the authoritative reference; a Chinese translation
layer is being added incrementally. Pages on the site cover
installation, a tutorial, an architecture overview, the CLI reference,
the auto-generated API reference, troubleshooting, a glossary, and
per-example walk-throughs.

The source for these pages lives under [`docs/`](docs/) and is built
with MkDocs Material.

## License

Apache-2.0.
