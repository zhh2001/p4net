---
description: A two-host p4net topology end to end, with the matching P4 program, in under 100 lines.
---

# Quick Start

This page walks through the smallest useful p4net program — two hosts
on a single switch, a port-swap dataplane, a successful ping. If you
have not yet installed p4net and its external dependencies, start
with [Installation](installation.md).

## What you'll build

```
+----+         +----+         +----+
| h1 |--eth0---|    |---eth0--| h2 |
+----+   1    | s1 |    2    +----+
              +----+
```

`h1` and `h2` are Linux hosts in private network namespaces. `s1` is
a `simple_switch_grpc` process running a port-swap pipeline: any
packet arriving on port 1 leaves on port 2, and vice versa. No
runtime table programming is needed; the dataplane is hardcoded.

## The Python topology

Save as `quickstart.py`:

```python
"""Minimal two-host p4net topology."""
from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()
h1 = topology.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topology.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topology.add_switch("s1", p4_src=HERE / "port_swap.p4")
topology.add_link(h1, s1, port_b=1)
topology.add_link(h2, s1, port_b=2)


def setup(net: Network) -> None:
    """Pre-seed static ARP so the first ICMP doesn't have to resolve."""
    h1, h2 = net.host("h1"), net.host("h2")
    h1.exec(["ip", "neigh", "replace", "10.0.0.2",
             "lladdr", "00:00:00:00:00:02",
             "dev", "h1-eth0", "nud", "permanent"])
    h2.exec(["ip", "neigh", "replace", "10.0.0.1",
             "lladdr", "00:00:00:00:00:01",
             "dev", "h2-eth0", "nud", "permanent"])
```

The `topology = Topology()` and `def setup(net)` shape is what the
`p4net` console script looks for: a module-level `topology` and an
optional `setup(net)` invoked after the network is up. Plain Python
scripts that build a `Network` directly work too.

## The P4 program

Save as `port_swap.p4` next to `quickstart.py`:

```p4
#include <core.p4>
#include <v1model.p4>

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

struct headers { ethernet_t ethernet; }
struct metadata {}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start { pkt.extract(hdr.ethernet); transition accept; }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyIngress(inout headers hdr, inout metadata meta,
                  inout standard_metadata_t std) {
    apply {
        if (std.ingress_port == 1) { std.egress_spec = 2; }
        else if (std.ingress_port == 2) { std.egress_spec = 1; }
        else { mark_to_drop(std); }
    }
}

control MyEgress(inout headers hdr, inout metadata meta,
                 inout standard_metadata_t std) { apply {} }

control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyDeparser(packet_out pkt, in headers hdr) {
    apply { pkt.emit(hdr.ethernet); }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(),
         MyComputeChecksum(), MyDeparser()) main;
```

## Run it

```
sudo p4net quickstart.py
```

The orchestrator will validate the topology, compile `port_swap.p4`,
create namespaces, wire veth pairs, configure addresses, start
`simple_switch_grpc` in the root namespace, push the compiled
pipeline via P4Runtime, run `setup(net)` to seed ARP, and drop you
into the interactive shell.

In the shell:

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  -            h1-eth0
h2    10.0.0.2/24  -            h2-eth0

p4net> pingall
H \ H   h1   h2
   h1    -    1
   h2    1    -
2/2 succeeded
```

Press `Ctrl-D` (or type `exit`) to tear the network down.

## What just happened

In order:

1. `Topology.validate()` ran (interface name length, IP collisions,
   asymmetric/symmetric impairment conflicts).
2. `P4Compiler` checked its content-addressed cache; on a miss it
   shells out to `p4c -b bmv2 --p4runtime-files=...`.
3. Network namespaces `h1` and `h2` were created.
4. veth pairs `h1-eth0 ↔ s1-eth1` and `h2-eth0 ↔ s1-eth2` were
   created in the root namespace, and the host-side ends were moved
   into their respective namespaces.
5. IPv4 addresses, MAC overrides, and per-interface IPv6 sysctls were
   applied; the interfaces were brought up.
6. `simple_switch_grpc` was launched with `--grpc-server-addr
   127.0.0.1:50051`, the orchestrator polled the gRPC port until it
   accepted connections, then pushed the pipeline config via
   `SetForwardingPipelineConfig`.
7. `setup(net)` ran static ARP entries.
8. The CLI took over. On exit, every step above was unwound in
   reverse — explicitly via the context manager, or as a fallback
   via the atexit hook.

The depth on each step lives on the [Architecture page](../architecture.md).

## Next steps

- The [Tutorial](../tutorial.md) walks through a 4-host, 2-switch,
  dual-stack topology with runtime table programming, asymmetric link
  impairment, and CPU-port packet I/O.
- The [Examples directory](../examples/index.md) ships six runnable
  topologies covering every v0.2.0 feature.
- The [CLI reference](../cli.md) documents every shell command.
