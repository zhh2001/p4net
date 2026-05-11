---
description: p4net is a Python framework for running real BMv2 dataplanes inside Linux network namespaces, controlled via P4Runtime, with a Mininet-shaped declarative API.
---

# p4net

> Mininet-style P4 SDN sandbox in Python.

## What it is

p4net brings up real `simple_switch_grpc` (BMv2) dataplanes inside Linux
network namespaces and wires them with `veth` pairs and `tc netem`
impairment, then drives them through P4Runtime — exactly the way an
operational P4 controller would talk to real hardware. The Python API
is shaped like Mininet's: declare a `Topology` of `Host`, `P4Switch`,
and `Link` objects, hand it to a `Network`, get a running playground
back.

The framework is geared at P4 development and education: classroom
labs, controller prototypes, regression harnesses for forwarding logic,
and quick experiments where spinning up a multi-host topology in a few
seconds matters more than emulating a specific switch ASIC. There is
no Docker, no OpenFlow, no Open vSwitch — the toolchain is `p4c`,
`simple_switch_grpc`, and the kernel's namespace primitives.

## What's in the box

- **Topology DSL** — `Host`, `P4Switch`, `Link`, `Topology` with
  static validation (interface name length, IP collisions, asymmetric
  vs symmetric impairment conflicts).
- **p4c-driven compilation** with a content-addressed cache at
  `~/.cache/p4net/compiler/` — recompile only when source or flags
  actually change.
- **BMv2 lifecycle management** — start, wait-for-ready, stop, gRPC
  port allocation, atexit / signal cleanup safety nets.
- **P4Runtime gRPC client** — table programming, indirect counter
  reads, multicast group management, CPU-port packet I/O over the
  StreamChannel.
- **Per-direction link impairment** via `tc netem` — `bandwidth`,
  `delay`, `jitter`, `loss_pct` either symmetric, split into
  `*_a_to_b` / `*_b_to_a`, or layered as a symmetric base plus a
  per-direction `*_extra`.
- **IPv4 and IPv6 host addressing** with per-interface sysctl gating
  (link-local addresses are suppressed on interfaces that don't ask
  for IPv6).
- **Interactive CLI** — `p4net <topology.py>` opens a shell with table
  dumps, ping matrices, packet send/listen, topology visualization.
- **Topology visualization** via `Topology.to_graphviz()` and
  `Topology.render_graphviz()` (DOT, PNG, SVG, PDF).
- **Unified `p4net.*` logger hierarchy** with documented level
  conventions and CLI verbosity flags (`-v` / `-vv`); see
  [Logging](logging.md).

## Install

```
pip install p4net
```

Detailed instructions including the external `p4c` and BMv2
prerequisites are on the [Installation page](getting-started/installation.md).

## 30-second example

```python
from pathlib import Path
from p4net import Network
from p4net.topo import Topology

topo = Topology()
h1 = topo.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topo.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topo.add_switch("s1", p4_src=Path("port_swap.p4"))
topo.add_link(h1, s1, port_b=1)
topo.add_link(h2, s1, port_b=2)

with Network(topo) as net:
    h1, h2 = net.host("h1"), net.host("h2")
    h1.exec(["ip", "neigh", "replace", "10.0.0.2",
             "lladdr", "00:00:00:00:00:02",
             "dev", "h1-eth0", "nud", "permanent"])
    print(h1.ping(h2))   # True
```

The `port_swap.p4` source is in [`examples/quick_start/`](examples/quick-start.md).

## Where to next

- **[Quick Start](getting-started/quickstart.md)** — the same example
  walked through end to end, with the P4 source.
- **[Tutorial](tutorial.md)** — a longer walk through dual-stack
  forwarding, runtime table programming, asymmetric link impairment,
  and CPU-port packet I/O.
- **[Architecture](architecture.md)** — what each package does and the
  design decisions behind the layout.
- **[Examples](examples/index.md)** — seven runnable topologies, from
  port-swap and L3 forwarding to in-band network telemetry (INT).
- **[CLI Reference](cli.md)** — every shell command with synopses and
  example output.
- **[API Reference](api/network.md)** — auto-generated from the Python
  docstrings.

## Project status

p4net is at **v1.3.0** — a stable release. The public API is
committed per [API Stability](api-stability.md): symbols marked
stable will not break in 1.x. Patch releases ship bug fixes; minor
releases add functionality additively. On top of the v1.0.0
stability baseline, v1.1.0 added single-switch INT, a unified
`p4net.*` logger hierarchy, and per-direction additive link
impairment; v1.2.0 added a register read/write API on
`P4RuntimeClient` and used it to make the INT example's `switch_id`
runtime-configurable; v1.3.0 added a multi-hop INT example showing
production-style stacked telemetry across two switches. The full
version history is in the [Changelog](changelog.md); known
boundaries are documented in
[Known Limitations](known-limitations.md); future plans are on the
[Roadmap](roadmap.md).

## License

[Apache-2.0](https://github.com/zhh2001/p4net/blob/main/LICENSE).
