---
description: Create one switch and two hosts, then verify bidirectional connectivity with a port-swap P4 program.
---

# Quick Start

This example creates two Linux hosts and one BMv2 switch, then checks both directions with `pingall`. Complete [Installation](installation.md) first, and run the commands below in the Python environment where p4net is installed.

```text
h1 (10.0.0.1/24) ---- 1 [ s1 ] 2 ---- h2 (10.0.0.2/24)
```

The switch forwards frames from port 1 to port 2 and vice versa. ARP and ICMP follow this path, so the hosts learn each other's MAC addresses without static neighbors or runtime table entries.

## Create two files

Create a working directory:

```bash
mkdir -p p4net-demo
cd p4net-demo
```

Save the following Python code as `quickstart.py`:

```python
from pathlib import Path

from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()
h1 = topology.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topology.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topology.add_switch("s1", p4_src=HERE / "port_swap.p4")
topology.add_link(h1, s1, port_b=1)
topology.add_link(h2, s1, port_b=2)
```

`port_b=1` and `port_b=2` select the switch-side port of each link. The host interfaces are `h1-eth0` and `h2-eth0`. `HERE` locates the P4 source relative to `quickstart.py`.

Save the following P4 program as `port_swap.p4` in the same directory:

```p4
#include <core.p4>
#include <v1model.p4>

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

struct headers {
    ethernet_t ethernet;
}

struct metadata {}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        pkt.extract(hdr.ethernet);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyIngress(inout headers hdr, inout metadata meta,
                  inout standard_metadata_t std) {
    apply {
        if (std.ingress_port == 1) {
            std.egress_spec = 2;
        } else if (std.ingress_port == 2) {
            std.egress_spec = 1;
        } else {
            mark_to_drop(std);
        }
    }
}

control MyEgress(inout headers hdr, inout metadata meta,
                 inout standard_metadata_t std) { apply {} }

control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyDeparser(packet_out pkt, in headers hdr) {
    apply {
        pkt.emit(hdr.ethernet);
    }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(),
         MyComputeChecksum(), MyDeparser()) main;
```

`MyIngress` selects the output from the ingress port and drops frames arriving on other ports. This program has no match tables; startup only needs to install the pipeline.

## Start the network

Run from the directory containing both files:

```bash
sudo "$(command -v p4net)" quickstart.py
```

This uses the `p4net` executable from the current environment, including a virtual environment. The CLI loads the module-level `topology`, compiles the P4 program, creates host namespaces and links, starts BMv2, and installs the pipeline through P4Runtime. It then displays the `p4net>` prompt.

If the topology file defines `setup(net)`, the CLI calls it after startup and before opening the shell. This example needs no extra initialization.

## Check connectivity

At the `p4net>` prompt, enter `hosts` followed by `pingall`:

```text
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  -           h1-eth0
h2    10.0.0.2/24  -           h2-eth0

p4net> pingall
H \ H h1  h2
h1  -   1
h2  1   -
2/2 succeeded
```

`hosts` lists the host addresses and interfaces. Matrix rows are senders and columns are receivers: `1` means success, `X` means failure, and `-` identifies the same host. `2/2 succeeded` means both `h1` to `h2` and `h2` to `h1` work.

## Exit and cleanup

Enter `exit`, or press `Ctrl-D` at an empty prompt. p4net closes the control connections, stops BMv2, and removes the namespaces and links it created.

See [Troubleshooting](../troubleshooting.md) if compilation, startup, or the connectivity check fails.

## Next steps

- [Tutorial](../tutorial.md): program runtime table entries and build a larger topology.
- [CLI reference](../cli.md): explore shell commands, parameters, and packet operations.
- [Examples](../examples/index.md): run more topologies with different dataplane features.
