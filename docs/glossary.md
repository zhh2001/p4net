---
description: Glossary of P4, SDN, Linux networking, and tooling terms used in the p4net documentation.
---

# Glossary

## P4 ecosystem

`P4_16`
:   The current major version of the P4 language (the data-plane
    programming language). p4net only consumes P4_16 sources.

`v1model`
:   The reference P4 architecture used by BMv2's `simple_switch` /
    `simple_switch_grpc`. Defines the parser → ingress →
    traffic-manager → egress → deparser shape p4net assumes.

`PSA`
:   Portable Switch Architecture. A more capable v1model successor.
    p4net does not target PSA in v0.x; see [Roadmap](roadmap.md).

`p4c`
:   The reference P4 compiler. p4net invokes `p4c -b bmv2
    --p4runtime-files=p4info.txtpb` to produce both the BMv2 JSON and
    the P4Info description.

`P4Info`
:   A protobuf message describing a compiled P4 program's tables,
    actions, counters, registers, and controller-header layouts. Lets
    a generic P4Runtime client target a specific pipeline by name.

`BMv2`
:   Behavioral Model version 2 — the reference software P4 dataplane.
    `simple_switch_grpc` is the v1model BMv2 binary with P4Runtime
    server enabled.

`simple_switch_grpc`
:   The specific BMv2 binary p4net uses. Listens on a configurable
    gRPC port (default 50051) for P4Runtime, plus an optional Thrift
    port (default 9090) for direct dataplane debugging.

`P4Runtime`
:   The standard control-plane protocol for P4 dataplanes. Defined in
    a protobuf schema; rides on gRPC. Covers pipeline configuration,
    table CRUD, counter reads, register reads, multicast group
    management, and a StreamChannel for asynchronous packet I/O.

## P4 dataplane primitives

`Match-Action Table`
:   A table of `(match key) → (action, params)` entries. The packet's
    field values match an entry; the entry's action runs with the
    stored params.

`LPM`
:   Longest Prefix Match. A match-key type used for IP routing
    tables; entries are `(prefix, prefix_length)`.

`Ternary`
:   Match-key type for value-and-mask matches (any subset of bits
    cared about). Requires a priority field for disambiguation.

`Range`
:   Match-key type for value-in-`[low, high]` matches. Requires a
    priority.

`Exact`
:   Match-key type for exact equality.

`Action`
:   A P4 routine that runs when a table entry matches; modifies
    headers, picks egress port, etc. Can be parameterised
    (`set_egress_port(bit<9> port)`).

`Counter`
:   A P4-side packet/byte counter. Direct counters are bound to a
    table; indirect counters are standalone arrays addressed by a
    32-bit index.

## Control-plane concepts

`Control plane`
:   The component(s) that decide what the dataplane should do —
    install table entries, read counters, react to packet-in events.
    For p4net, the control plane is your Python topology + setup
    code.

`Data plane`
:   The component that processes individual packets at line rate.
    For p4net, the dataplane is `simple_switch_grpc`.

`Pipeline`
:   The compiled dataplane program currently running on a switch
    (parser + controls + deparser). Pushed via P4Runtime's
    `SetForwardingPipelineConfig`.

`Election ID`
:   A 128-bit number that orders P4Runtime controllers competing for
    primary mastership. Highest election ID wins. p4net uses
    millisecond-since-epoch so re-running the same script always
    claims primary.

`Mastership`
:   Which controller holds the primary role for a device.
    P4Runtime allows one primary writer at a time.

## Linux networking

`Network namespace` (`netns`)
:   A kernel-isolated network stack: separate interfaces, routing
    tables, IP addresses, neighbor caches. p4net puts each host in
    its own.

`veth pair`
:   A pair of virtual Ethernet interfaces wired to each other —
    packets out one show up on the other. p4net uses these to wire
    hosts (in their namespace) to switches (in the root namespace).

`qdisc`
:   Queueing discipline. Linux's traffic-control state machine on an
    interface; controls scheduling, shaping, dropping.

`netem`
:   Network emulation `qdisc` — adds configurable delay, jitter,
    loss, reorder, corruption.

`tc`
:   The userspace tool that programs `qdisc`. p4net shells out to
    `tc qdisc add ... root netem ...` from
    `p4net.runtime.apply_netem`.

`sysctl`
:   The userspace tool that reads/writes kernel `/proc/sys` knobs.
    p4net uses `sysctl -w net.ipv6.conf.<iface>.disable_ipv6=...` for
    its per-interface IPv6 gate.

`Link-local address`
:   A non-routable IPv6 address (`fe80::/10`) that interfaces
    auto-generate when IPv6 is enabled. p4net suppresses these on
    interfaces that don't ask for IPv6.

`SLAAC`
:   StateLess Address AutoConfiguration — IPv6 hosts deriving
    addresses from Router Advertisements. Disabled by default in
    p4net (`accept_ra=0`, `autoconf=0`) so explicitly assigned
    addresses are the only ones present.

`ND` (Neighbor Discovery)
:   IPv6's analogue of ARP. Resolves IPv6 addresses to MACs.

`ARP`
:   Address Resolution Protocol. Resolves IPv4 addresses to MACs.
    p4net's examples seed static ARP entries because BMv2 has no
    built-in ARP responder.

## Tooling and packaging

`PyPI`
:   The Python Package Index. p4net publishes wheels here as
    `p4net`.

`MkDocs`
:   The static-site generator p4net uses for this documentation
    site.

`Material for MkDocs`
:   The MkDocs theme used here. Provides search, dark mode, social
    cards, and the navigation chrome.

`mkdocstrings`
:   The MkDocs plugin that generates the API reference from Python
    docstrings.

`Graphviz DOT`
:   The graph description language `Topology.to_graphviz()` emits.
    The `dot` command renders DOT to PNG/SVG/PDF.
