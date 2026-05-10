---
description: A four-host, two-switch p4net walk-through covering IPv4 and IPv6 LPM forwarding, asymmetric link impairment, and CPU-port packet I/O.
---

# Tutorial

This tutorial builds a richer topology than [Quick Start](getting-started/quickstart.md):
four hosts, two switches, IPv4 and IPv6 dual-stack, asymmetric link
delay, and a CPU-port punt path that lets the controller observe
unmatched packets. It exercises every v0.2.0 feature in one program.

If you have not yet installed p4net and its external dependencies,
start with [Installation](getting-started/installation.md).

## What we're building

```
                 +----+
                 | h1 | 10.0.0.1/24, fd00::1/64
                 +-+--+
                   | (h1↔s1 link with delay_a_to_b="50ms")
                 +-+--+        +----+
                 | s1 |--------| s2 |
                 +-+--+        +-+--+
                   |             |
                 +-+--+        +-+--+
        h2/h3 -- |    |        |    | -- h4
                 ...both attach to s1 / s2 respectively...
```

Concretely:

- `h1` and `h2` attach to `s1`. `h1`'s link has 50 ms delay one-way
  (h1 → s1).
- `h3` and `h4` attach to `s2`.
- `s1` ↔ `s2` is a backbone link with 100 ms delay one-way (s1 → s2).
- Every host has IPv4 and IPv6 addresses on the same `/24` / `/64`.
- The pipeline forwards IPv4 via an `ipv4_lpm` table and IPv6 via an
  `ipv6_lpm` table, both programmed at runtime. Unmatched packets get
  punted to the controller via the CPU port.

## Step 1: scaffold the topology

Save as `tutorial.py`:

```python
"""p4net tutorial: dual-stack forwarding with asymmetric impairment."""
from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent
P4_SRC = HERE / "tutorial.p4"

topology = Topology()

h1 = topology.add_host("h1", ip="10.0.0.1/24", ip6="fd00::1/64",
                       mac="00:00:00:00:00:01")
h2 = topology.add_host("h2", ip="10.0.0.2/24", ip6="fd00::2/64",
                       mac="00:00:00:00:00:02")
h3 = topology.add_host("h3", ip="10.0.0.3/24", ip6="fd00::3/64",
                       mac="00:00:00:00:00:03")
h4 = topology.add_host("h4", ip="10.0.0.4/24", ip6="fd00::4/64",
                       mac="00:00:00:00:00:04")

s1 = topology.add_switch("s1", p4_src=P4_SRC, cpu_port=510)
s2 = topology.add_switch("s2", p4_src=P4_SRC, cpu_port=510)
```

Both switches load the same `tutorial.p4` source — the compiler cache
ensures it's compiled once. `cpu_port=510` is BMv2's convention for
punt: packets directed at port 510 surface as `PacketIn` to the
controller.

## Step 2: links with asymmetric delay

```python
# Hosts on s1.
topology.add_link(h1, s1, port_b=1, delay_a_to_b="50ms")
topology.add_link(h2, s1, port_b=2)
# Hosts on s2.
topology.add_link(h3, s2, port_b=1)
topology.add_link(h4, s2, port_b=2)
# Backbone.
topology.add_link(s1, s2, port_a=3, port_b=3, delay_a_to_b="100ms")
```

`delay_a_to_b="50ms"` on the h1↔s1 link applies `tc netem delay 50ms`
to the `a`-side veth — the one in `h1`'s namespace. Egress from `h1`
toward `s1` picks up 50 ms; the reverse direction is unshaped.
Similarly `delay_a_to_b="100ms"` on the s1↔s2 link delays only the
s1 → s2 direction. So one-way `h1 → h3` accumulates 50 ms (h1→s1) +
100 ms (s1→s2) + 0 ms (s2→h3) = 150 ms. The reverse h3 → h1 is
unshaped, so the ping RTT from h1 to h3 is ~150 ms with sub-ms
jitter.

## Step 3: the P4 program

Save as `tutorial.p4`:

```p4
#include <core.p4>
#include <v1model.p4>

const bit<9> CPU_PORT = 510;

@controller_header("packet_in")
header packet_in_t { bit<9> ingress_port; bit<7> _pad0; }

@controller_header("packet_out")
header packet_out_t { bit<9> egress_port; bit<7> _pad0; }

header ethernet_t { bit<48> dstAddr; bit<48> srcAddr; bit<16> etherType; }
header ipv4_t {
    bit<4> version; bit<4> ihl; bit<8> diffserv; bit<16> totalLen;
    bit<16> identification; bit<3> flags; bit<13> fragOffset;
    bit<8> ttl; bit<8> protocol; bit<16> hdrChecksum;
    bit<32> srcAddr; bit<32> dstAddr;
}
header ipv6_t {
    bit<4> version; bit<8> trafficClass; bit<20> flowLabel;
    bit<16> payloadLen; bit<8> nextHdr; bit<8> hopLimit;
    bit<128> srcAddr; bit<128> dstAddr;
}

struct headers {
    packet_in_t  packet_in;
    packet_out_t packet_out;
    ethernet_t   ethernet;
    ipv4_t       ipv4;
    ipv6_t       ipv6;
}
struct metadata {}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        transition select(std.ingress_port) {
            CPU_PORT: parse_packet_out;
            default:  parse_ethernet;
        }
    }
    state parse_packet_out { pkt.extract(hdr.packet_out); transition parse_ethernet; }
    state parse_ethernet {
        pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            0x0800: parse_ipv4;
            0x86DD: parse_ipv6;
            default: accept;
        }
    }
    state parse_ipv4 { pkt.extract(hdr.ipv4); transition accept; }
    state parse_ipv6 { pkt.extract(hdr.ipv6); transition accept; }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyIngress(inout headers hdr, inout metadata meta,
                  inout standard_metadata_t std) {
    action drop() { mark_to_drop(std); }
    action set_egress_port(bit<9> port) { std.egress_spec = port; }
    action punt() {
        std.egress_spec = CPU_PORT;
        hdr.packet_in.setValid();
        hdr.packet_in.ingress_port = std.ingress_port;
        hdr.packet_in._pad0 = 0;
    }
    table ipv4_lpm {
        key = { hdr.ipv4.dstAddr: lpm; }
        actions = { drop; set_egress_port; punt; }
        default_action = punt();
        size = 1024;
    }
    table ipv6_lpm {
        key = { hdr.ipv6.dstAddr: lpm; }
        actions = { drop; set_egress_port; punt; }
        default_action = punt();
        size = 1024;
    }
    apply {
        if (std.ingress_port == CPU_PORT) {
            std.egress_spec = hdr.packet_out.egress_port;
            hdr.packet_out.setInvalid();
        } else if (hdr.ipv4.isValid()) {
            ipv4_lpm.apply();
        } else if (hdr.ipv6.isValid()) {
            ipv6_lpm.apply();
        }
    }
}

control MyEgress(inout headers hdr, inout metadata meta,
                 inout standard_metadata_t std) { apply {} }
control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyDeparser(packet_out pkt, in headers hdr) {
    apply {
        pkt.emit(hdr.packet_in);
        pkt.emit(hdr.ethernet);
        pkt.emit(hdr.ipv4);
        pkt.emit(hdr.ipv6);
    }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(),
         MyComputeChecksum(), MyDeparser()) main;
```

The interesting bits:

- The two LPM tables share a `default_action = punt()`. Anything
  that misses both tables flows to the CPU port with the `packet_in`
  controller header populated.
- `set_egress_port(port)` is parameterised, so the controller can
  install routes for any host without a per-host action.
- A controller-injected packet (parsed via the `packet_out` header) is
  forwarded to `hdr.packet_out.egress_port` and the controller header
  is invalidated before deparse so the wire packet looks normal.

## Step 4: program tables and seed neighbours

Back in `tutorial.py`:

```python
def setup(net: Network) -> None:
    """Pre-seed ARP/ND, install LPM entries on both switches."""
    h1 = net.host("h1")
    h2 = net.host("h2")
    h3 = net.host("h3")
    h4 = net.host("h4")

    # Static ARP/ND. With four hosts on the same /24 and /64, every
    # host can reach every other; seed them all.
    arp_pairs = [
        (h1, "10.0.0.2", "00:00:00:00:00:02"),
        (h1, "10.0.0.3", "00:00:00:00:00:03"),
        (h1, "10.0.0.4", "00:00:00:00:00:04"),
        (h2, "10.0.0.1", "00:00:00:00:00:01"),
        (h2, "10.0.0.3", "00:00:00:00:00:03"),
        (h2, "10.0.0.4", "00:00:00:00:00:04"),
        (h3, "10.0.0.1", "00:00:00:00:00:01"),
        (h3, "10.0.0.2", "00:00:00:00:00:02"),
        (h3, "10.0.0.4", "00:00:00:00:00:04"),
        (h4, "10.0.0.1", "00:00:00:00:00:01"),
        (h4, "10.0.0.2", "00:00:00:00:00:02"),
        (h4, "10.0.0.3", "00:00:00:00:00:03"),
    ]
    for host, ip4, mac in arp_pairs:
        iface = next(iter(host.interfaces))
        host.exec(["ip", "neigh", "replace", ip4, "lladdr", mac,
                   "dev", iface, "nud", "permanent"])
    # Mirror the ARP entries as IPv6 ND.
    for host, ip4, mac in arp_pairs:
        ip6 = "fd00::" + ip4.split(".")[-1]
        iface = next(iter(host.interfaces))
        host.exec(["ip", "-6", "neigh", "replace", ip6, "lladdr", mac,
                   "dev", iface, "nud", "permanent"])

    # Forwarding tables: s1 routes h1/h2 locally, h3/h4 via the
    # backbone (port 3). s2 mirrors.
    s1 = net.switch("s1")
    s2 = net.switch("s2")

    for table_name, dst_prefix, port in [
        ("MyIngress.ipv4_lpm", "10.0.0.1/32", 1),
        ("MyIngress.ipv4_lpm", "10.0.0.2/32", 2),
        ("MyIngress.ipv4_lpm", "10.0.0.3/32", 3),
        ("MyIngress.ipv4_lpm", "10.0.0.4/32", 3),
    ]:
        s1.client.insert_table_entry(
            table=table_name,
            match={"hdr.ipv4.dstAddr": dst_prefix},
            action="MyIngress.set_egress_port",
            params={"port": port},
        )
    for table_name, dst_prefix, port in [
        ("MyIngress.ipv4_lpm", "10.0.0.1/32", 3),
        ("MyIngress.ipv4_lpm", "10.0.0.2/32", 3),
        ("MyIngress.ipv4_lpm", "10.0.0.3/32", 1),
        ("MyIngress.ipv4_lpm", "10.0.0.4/32", 2),
    ]:
        s2.client.insert_table_entry(
            table=table_name,
            match={"hdr.ipv4.dstAddr": dst_prefix},
            action="MyIngress.set_egress_port",
            params={"port": port},
        )
    # Same scheme for IPv6.
    for table_name, dst_prefix, port in [
        ("MyIngress.ipv6_lpm", "fd00::1/128", 1),
        ("MyIngress.ipv6_lpm", "fd00::2/128", 2),
        ("MyIngress.ipv6_lpm", "fd00::3/128", 3),
        ("MyIngress.ipv6_lpm", "fd00::4/128", 3),
    ]:
        s1.client.insert_table_entry(
            table=table_name,
            match={"hdr.ipv6.dstAddr": dst_prefix},
            action="MyIngress.set_egress_port",
            params={"port": port},
        )
    for table_name, dst_prefix, port in [
        ("MyIngress.ipv6_lpm", "fd00::1/128", 3),
        ("MyIngress.ipv6_lpm", "fd00::2/128", 3),
        ("MyIngress.ipv6_lpm", "fd00::3/128", 1),
        ("MyIngress.ipv6_lpm", "fd00::4/128", 2),
    ]:
        s2.client.insert_table_entry(
            table=table_name,
            match={"hdr.ipv6.dstAddr": dst_prefix},
            action="MyIngress.set_egress_port",
            params={"port": port},
        )


if __name__ == "__main__":
    from p4net.cli.main import main
    raise SystemExit(main([__file__]))
```

## Step 5: install a packet-in handler (optional)

For an interactive look at the punt path, register a handler before
running the shell. The handler runs on the StreamChannel consumer
thread; keep it short and fast.

```python
def setup(net: Network) -> None:
    # ... (everything above) ...
    def log_punt(payload: bytes, metadata: dict[str, int]) -> None:
        port = metadata.get("ingress_port", "?")
        # Truncate to keep the log line readable.
        head = payload[:32].hex()
        net.host("h1").exec(  # log via stdout in h1's namespace
            ["logger", "-t", "p4net-punt", f"port={port} head={head}"])
    net.switch("s1").client.on_packet_in(log_punt)
    net.switch("s2").client.on_packet_in(log_punt)
```

In production a controller would parse the punted Ethernet frame,
make a decision, and reinject via `send_packet_out`. This handler
just records the event.

## Step 6: run and explore

```
sudo p4net tutorial.py
```

In the shell:

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  fd00::1/64   h1-eth0
h2    10.0.0.2/24  fd00::2/64   h2-eth0
h3    10.0.0.3/24  fd00::3/64   h3-eth0
h4    10.0.0.4/24  fd00::4/64   h4-eth0

p4net> pingall
H \ H   h1   h2   h3   h4
   h1    -    1    1    1
   h2    1    -    1    1
   h3    1    1    -    1
   h4    1    1    1    -
12/12 succeeded

p4net> pingall6
H \ H   h1   h2   h3   h4
   h1    -    1    1    1
   ...
12/12 succeeded

p4net> s1 table dump MyIngress.ipv4_lpm
#0
  table:    MyIngress.ipv4_lpm
  match:    {'hdr.ipv4.dstAddr': '10.0.0.1/32'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
#1
  ...

p4net> s1 table dump MyIngress.ipv6_lpm
#0
  table:    MyIngress.ipv6_lpm
  match:    {'hdr.ipv6.dstAddr': 'fd00::1/128'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
  ...
```

Note the table dump renders IPv6 LPM matches as `fd00::1/128` — not as
raw bytes. That's `P4InfoIndex.decode_match` doing the round-trip
through `decode_ipv6`.

## Step 7: measure the asymmetric link

`h1 → h3` traverses two shaped links: 50 ms (h1 → s1) plus 100 ms
(s1 → s2). The reverse direction is unshaped. So ping RTT ≈ 150 ms.

```
p4net> h1 ping h3 5 3
PING 10.0.0.3 (10.0.0.3) 56(84) bytes of data.
64 bytes from 10.0.0.3: icmp_seq=1 ttl=64 time=151 ms
64 bytes from 10.0.0.3: icmp_seq=2 ttl=64 time=151 ms
...
rtt min/avg/max/mdev = 151.012/151.187/151.412/0.392 ms
```

`h2 → h3` is unshaped on h2's side and only crosses the 100 ms
backbone link, so RTT ≈ 100 ms. `h1 → h2` is shaped only on h1's
egress, so RTT ≈ 50 ms.

## Step 8: visualize

```
p4net> topology graph /tmp/topo.png
/tmp/topo.png
```

The orchestrator runs `Topology.validate()`, then pipes the DOT
source through `dot -Tpng`. Open `/tmp/topo.png` in an image viewer
to see the rendered graph.

If `dot` isn't installed, use `format=dot` to emit the source
verbatim:

```
p4net> topology graph /tmp/topo.dot format=dot
/tmp/topo.dot
```

## Where to go next

- The [Examples directory](examples/index.md) ships six runnable
  topologies — each one isolates a single feature, which is sometimes
  easier to read than this kitchen-sink tutorial.
- The [API reference](api/network.md) documents every class and
  function used above.
- The [Roadmap](roadmap.md) lists v0.3.0 candidates including PSA
  architecture support and an async P4Runtime client.
