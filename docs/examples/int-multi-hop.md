---
description: Multi-hop INT demo. Two switches in series, both running the same P4, each insert their own metadata shim. The receiver decodes the full hop-by-hop chain.
---

# Multi-hop INT (in-band telemetry)

Two switches in series, each inserting its own 14-byte INT shim header
into every forwarded packet. The receiver decodes the full hop-by-hop
stack to reconstruct the packet's journey across the topology. This is
the production-style INT example; for the simpler single-switch
introduction see [INT (in-band telemetry)](int.md).

## What this demonstrates

- **Hop-by-hop metadata accumulation**: every switch on the path
  inserts its own shim, so the egress receiver sees one block of
  metadata per traversed switch.
- **Shim chaining via `next_proto`**: each shim's `next_proto` field
  names the next header in order. The parser walks
  `etherType → shim_1.next_proto → shim_2.next_proto → ipv4`. No
  P4 header stack required for the two-hop case.
- **Per-switch identity from a register**: the same P4 program runs on
  both switches; per-switch `switch_id` comes from the v1.2 register
  API via `write_register("MyIngress.switch_id_reg", index=0, value=N)`
  at start-up.

## Topology

`examples/int_multi_hop/topology.py`:

```python
--8<-- "examples/int_multi_hop/topology.py"
```

Four nodes, three links, linear path: `h1 — s1 — s2 — h2`.

## P4 program

`examples/int_multi_hop/int_multi_hop.p4`:

```p4
--8<-- "examples/int_multi_hop/int_multi_hop.p4"
```

Key points:

- Two named header instances `int_shim_1` and `int_shim_2` instead of
  a P4 header stack. Easier to read at two hops; for N hops, see the
  "Extending" section in the example README.
- Ingress picks the first unfilled shim slot and writes it from
  `standard_metadata` plus the configured `switch_id`. The
  `next_proto` chain is re-stitched so the receiver sees
  `eth → shim_1 → shim_2 → ipv4`.
- The deparser emits every valid header in declaration order.

## The listener

`examples/int_multi_hop/listener.py`:

```python
--8<-- "examples/int_multi_hop/listener.py"
```

The listener walks the shim chain starting from the outer EtherType,
parsing one 14-byte shim per hop until `next_proto` leaves the INT
space.

## Run it

In one terminal:

```
sudo p4net examples/int_multi_hop/topology.py
```

`setup(net)` installs the L2 forwarding tables on both switches,
pre-seeds static ARP on both hosts, and writes each switch's identity
register. Drop into the `p4net>` shell.

In a second terminal (or `h2 xterm` from the shell):

```
sudo ip netns exec h2 python3 examples/int_multi_hop/listener.py --iface h2-eth0
```

From a third terminal:

```
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

The listener prints one block per packet, with one `hop` line per
traversed switch (two lines per packet in this topology).

## Sample output

Captured by the multi-hop integration test:

```
packet (2 hop(s), final proto 0x0800): 10.0.0.1 -> 10.0.0.2
  hop 1: switch_id=1 ts=874083us egress_port=2 queue_depth=0
  hop 2: switch_id=2 ts=769824us egress_port=2 queue_depth=0
packet (2 hop(s), final proto 0x0800): 10.0.0.1 -> 10.0.0.2
  hop 1: switch_id=1 ts=1875211us egress_port=2 queue_depth=0
  hop 2: switch_id=2 ts=1770811us egress_port=2 queue_depth=0
```

`hop 1` is s1 (egress port 2 toward s2); `hop 2` is s2 (egress port 2
toward h2). The listener prints hops in insertion order, so the first
switch the packet entered always appears as `hop 1`.

## What's interesting

- **Timestamps are per-switch-process.** BMv2's
  `standard_metadata.ingress_global_timestamp` starts at zero when each
  `simple_switch_grpc` process boots. The two shims' timestamps are
  not directly comparable; their difference reflects per-process boot
  skew, not wire latency. For real per-link latency you need a shared
  time reference (PTP-style) or a controller-mediated boot offset.
- **Egress ports correspond to the path direction.** s1 forwards out
  port 2 toward s2; s2 forwards out port 2 toward h2. Different
  topologies produce different numbers.
- **`queue_depth` is reliably 0** at this offered load — BMv2's
  default queueing doesn't surface non-zero values without explicit
  configuration and saturation.

## Caveats

- **Two hops only with the current pipeline.** A third switch on the
  path would find both shim slots full and forward without further
  annotation. Real deployments use a P4 header stack of MAX_HOPS depth
  — see the example README for the rewrite recipe.
- **`queue_depth` is almost always 0.** Same as the single-switch
  example.
- **No checksum recomputation for the inserted shims.** The IPv4
  checksum covers only the IPv4 header; the shim layer between
  Ethernet and IPv4 is unprotected, matching how production INT works
  (the INT spec assumes link-layer integrity).
