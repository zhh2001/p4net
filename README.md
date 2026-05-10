# p4net

A P4Runtime-native SDN simulation framework for BMv2.

Status: Pre-alpha — under active development. APIs are unstable.

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

Not yet published. Clone and `pip install -e '.[dev]'`.

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
network, and drops you into an interactive shell.

## License

Apache-2.0.
