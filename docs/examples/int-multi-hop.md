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

Captured by the v1.4 multi-hop integration test (aligned mode):

```
packet (2 hop(s), final proto 0x0800): 10.0.0.1 -> 10.0.0.2
  hop 1: switch_id=1 ts=800454us aligned=1778513670403185us egress_port=2 queue_depth=0
  hop 2: switch_id=2 ts=699418us aligned=1778513670403875us egress_port=2 queue_depth=0
  latency_s1_to_s2 = 690us
```

`hop 1` is s1; `hop 2` is s2. Each `ts` is BMv2's per-process
`ingress_global_timestamp`; `aligned` is wall-clock μs since Unix
epoch; `latency_s1_to_s2` is the wall-clock delta between aligned
arrival times — real per-hop forwarding latency through BMv2's
userspace pipeline plus the veth pair.

Running the listener directly without `setup(net)` (so no coordination
file is present) falls back to the v1.3 unaligned display: raw `ts`,
no `aligned=` line, no latency.

## How cross-switch timestamp alignment works

BMv2's `standard_metadata.ingress_global_timestamp` is **per-process**:
each `simple_switch_grpc` instance's clock starts at zero on boot, so
raw `shim_1.ts` and `shim_2.ts` aren't directly comparable across
hops. Since v1.4, every `RunningSwitch` exposes a `boot_timestamp_us`
property (wall-clock μs since Unix epoch at process start, captured
immediately before `subprocess.Popen`). The alignment formula:

```
wall_clock_us = switch.boot_timestamp_us + shim.ingress_timestamp_us
```

`setup(net)` writes both switches' boot timestamps to a JSON
coordination file at `/tmp/p4net-int-multi-hop-boot-times.json`; the
listener reads it at startup and prints `aligned=...us` next to each
raw `ts`. Subtraction across hops gives the `latency_s1_to_s2` line.

Drift is bounded by Popen + early-init overhead — sub-millisecond
typically, occasionally a couple of milliseconds under load. Good
enough for μs-vs-ms regime decisions; for serious latency research
use a real shared time source (PTP).

## What's interesting

- **Per-hop forwarding latency is now observable.** The
  `latency_s1_to_s2` line ranges from a few hundred microseconds to
  a few milliseconds on this rig. Real ASIC switches are 10–100×
  faster; BMv2's userspace interpreter is the bottleneck.
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
- **Alignment drift is sub-millisecond.** `boot_timestamp_us` is
  captured immediately before `Popen`, but BMv2's actual internal
  clock zero is slightly later. Good enough for μs/ms regime checks,
  not good enough for nanosecond-scale latency research; use PTP for
  that.
- **Listener relies on a `/tmp/` coordination file.** Concurrent
  multi-hop INT topologies on the same host would trample each other's
  files. The example assumes one topology at a time.
- **`queue_depth` is almost always 0.** Same as the single-switch
  example.
- **No checksum recomputation for the inserted shims.** The IPv4
  checksum covers only the IPv4 header; the shim layer between
  Ethernet and IPv4 is unprotected, matching how production INT works
  (the INT spec assumes link-layer integrity).
