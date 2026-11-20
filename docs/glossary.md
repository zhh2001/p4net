---
description: Glossary of P4, SDN, Linux networking, and tooling terms used in the p4net documentation.
---

# Glossary

## P4 ecosystem

`P4_16`
:   The version of the P4 programming language used by P4Net's example programs.

`v1model`
:   The P4 architecture used by BMv2's `simple_switch` and `simple_switch_grpc`. It defines the parser, ingress, traffic manager, egress, and deparser stages.

`PSA`
:   Portable Switch Architecture. A P4 architecture that defines packet processing blocks and common externs for programmable switches. p4net currently supports only the `v1model` switch architecture. See [P4 architecture support](known-limitations.md#3-psa-architecture-not-supported).

`p4c`
:   The P4 compiler P4Net uses to generate BMv2 JSON and P4Info.

`P4Info`
:   A protobuf message describing a compiled P4 program's tables, actions, counters, registers, and controller-header layouts. Lets a generic P4Runtime client target a specific pipeline by name.

`BMv2`
:   Behavioral Model version 2, a reference P4 software switch. The `simple_switch_grpc` target runs v1model programs and provides a P4Runtime server.

`simple_switch_grpc`
:   The BMv2 executable P4Net starts. The first switch defaults to port 50051 for P4Runtime over gRPC and port 9090 for Thrift. Both ports can be set per switch.

`P4Runtime`
:   A gRPC protocol for controlling P4 devices. It supports pipeline configuration, table entries, counters, registers, multicast groups, and packet I/O through the StreamChannel.

## P4 dataplane primitives

`Match-Action Table`
:   A table of `(match key) → (action, params)` entries. The packet's field values match an entry; the entry's action runs with the stored params.

`LPM`
:   Longest Prefix Match. A match-key type used for IP routing tables; entries are `(prefix, prefix_length)`.

`Ternary`
:   Match-key type for value-and-mask matches (any subset of bits cared about). Requires a priority field for disambiguation.

`Range`
:   Match-key type for value-in-`[low, high]` matches. Requires a priority.

`Exact`
:   Match-key type for exact equality.

`Action`
:   A P4 routine that runs when a table entry matches; modifies headers, picks egress port, etc. Can be parameterised (`set_egress_port(bit<9> port)`).

`Counter`
:   A P4-side packet/byte counter. Direct counters are bound to a table; indirect counters are standalone arrays addressed by a 32-bit index.

## Control-plane concepts

`Control plane`
:   The software that configures and monitors packet forwarding. In P4Net, a Python controller or the CLI can install table entries, read counters, and receive PacketIn messages.

`Data plane`
:   The part of a switch that processes packets using the loaded forwarding program. P4Net runs this program in BMv2.

`Pipeline`
:   The compiled dataplane program currently running on a switch (parser + controls + deparser). Pushed via P4Runtime's `SetForwardingPipelineConfig`.

`Election ID`
:   A 128-bit unsigned integer a controller supplies during P4Runtime arbitration. Higher IDs take priority within a device and role. `Network` uses Unix time in milliseconds for the high 64 bits and zero for the low 64 bits. `AsyncP4RuntimeClient` uses the same scheme by default. IDs generated in the same millisecond can coincide. See [Connections and primary status](async-client.md#connections-and-primary-status) to set an ID explicitly.

`Mastership`
:   Primary status for a device and role. P4Runtime elects at most one primary for each `(device_id, role)` pair. The primary can write within that role's permissions. See [P4Runtime client arbitration](https://p4lang.github.io/p4runtime/spec/v1.5.0/P4Runtime-Spec.html#sec-client-arbitration-and-controller-replication).

## Linux networking

`Network namespace` (`netns`)
:   A network stack with its own interfaces, addresses, routes, and neighbor entries. P4Net creates one network namespace per host.

`veth pair`
:   Two connected virtual Ethernet interfaces. A packet sent through one arrives at the other. P4Net uses them to connect nodes in a topology.

`qdisc`
:   Queueing discipline. A Linux traffic-control rule that determines how an interface queues, schedules, or drops packets.

`netem`
:   A Linux queueing discipline for emulating delay, jitter, packet loss, reordering, and corruption.

`tc`
:   The Linux command for configuring traffic control, including queueing disciplines such as `netem`.

`sysctl`
:   The Linux command for reading and changing kernel settings under `/proc/sys`. P4Net uses `net.ipv6.conf.<iface>.disable_ipv6` to enable or disable IPv6 on individual interfaces.

`Link-local address`
:   An IPv6 address in `fe80::/10` used on the local link. Linux can generate one when IPv6 is enabled on an interface, depending on its `addr_gen_mode` setting.

`SLAAC`
:   Stateless Address Autoconfiguration. p4net sets `accept_ra=0` and `autoconf=0` on IPv6-enabled host interfaces to disable address autoconfiguration from router advertisements.

`ND` (Neighbor Discovery)
:   The IPv6 protocol for discovering routers and neighbors, including resolving IPv6 addresses to link-layer addresses.

`ARP`
:   Address Resolution Protocol. Resolves an IPv4 address to a MAC address on the local link. The [L3 forwarding example](examples/l3-forwarding.md) configures static ARP entries for its IPv4 forwarding pipeline.

## Tooling and packaging

`PyPI`
:   The Python Package Index. P4Net is published here as `p4net`.

`Graphviz DOT`
:   The graph description language `Topology.to_graphviz()` emits. The `dot` command renders DOT to PNG/SVG/PDF.
