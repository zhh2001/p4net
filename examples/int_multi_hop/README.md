# Multi-hop INT (in-band network telemetry)

Two switches in series, each inserting its own 14-byte INT shim header
into every forwarded packet. The receiver decodes the full hop-by-hop
metadata chain to reconstruct the packet's journey across the topology.

This is the "production-style" INT example. For the simpler single-switch
introduction, see [`examples/int/`](../int/) — same shim format, one
switch, easier to read.

## What this demonstrates

- **Hop-by-hop metadata accumulation.** Every switch on the packet's
  path inserts its own INT shim. After traversing N switches the
  packet carries an N-shim stack between Ethernet and IPv4.
- **Shim chaining via `next_proto`.** Each shim's `next_proto` field
  names the next header in order. The parser and listener walk the
  chain via `etherType → shim_1.next_proto → shim_2.next_proto →
  ipv4`. No P4 header stack required for the 2-hop case.
- **Per-switch identity from a register.** The same P4 program runs on
  both switches; each switch's `switch_id` comes from the v1.2 register
  API, written at start-up in `setup(net)`.

## Why this is the realistic example

Single-switch INT (see `examples/int/`) shows the *mechanism* — how a
P4 deparser injects a header outside the standard ethertype range. But
real INT deployments do something more interesting: every transit device
on a path inserts its own metadata, so the egress switch (or the
receiver) can reconstruct what every hop saw. Multi-hop INT is the
shape of telemetry that's been productized.

## Topology

```
  h1 (10.0.0.1/24, 00:00:00:00:00:01)
   |
   | port 1
   |
   +------+
   |  s1  |   switch_id=1
   +------+
   | port 2
   |
   | port 1
   +------+
   |  s2  |   switch_id=2
   +------+
   | port 2
   |
  h2 (10.0.0.2/24, 00:00:00:00:00:02)
```

Four nodes, three links, no branching. Same P4 binary loaded on both
switches; per-switch identity supplied by the register write.

## The P4 pipeline

`int_multi_hop.p4`:

- **Three headers**: `ethernet_t`, `int_shim_t` (two named instances —
  `int_shim_1` and `int_shim_2`), and `ipv4_t`.
- **Parser** does recursive descent on `etherType` / `next_proto`,
  validating shim headers as it encounters them. At the IPv4 boundary
  it transitions to `parse_ipv4` and stops.
- **Ingress** runs `l2_forward.apply()` to set `std.egress_spec`. If
  forwarding succeeded, it picks the first unfilled shim slot
  (`int_shim_1` then `int_shim_2`) and populates it from
  `standard_metadata` plus the configured `switch_id`. It then
  re-stitches the `next_proto` chain so the receiver sees
  `eth → shim_1 → shim_2 → ipv4`.
- **Deparser** emits every valid header in declaration order. Invalid
  shim headers are skipped entirely; the wire bytes never contain
  unused 14-byte gaps.

## Multi-hop vs header stack

This example uses two named header instances rather than a P4 header
stack. The trade-off:

| Aspect | Named instances (this example) | Header stack |
| ------ | ------------------------------ | ------------ |
| Max hops | Fixed at 2 (or whatever N you write out) | Configurable at compile time |
| Parser | Explicit transitions; easy to read | `pkt.extract(stack.next)` in a loop |
| Ingress | Explicit `if/else if` chain | `stack.push_front(1)` |
| Pedagogy | Clearer for 2 hops | More general |

For a real deployment with variable hop counts, replace `int_shim_1`
and `int_shim_2` with `int_shim_t[MAX_HOPS] shims`, use
`shims.push_front(1)` in ingress, and loop the parser through up to
`MAX_HOPS` extractions. The "Extending to N hops" section below has
the full recipe.

## Per-switch parameterization

The P4 program runs unchanged on both switches. Per-switch identity
comes from a register written at start-up:

```python
s1_rt.client.write_register("MyIngress.switch_id_reg", index=0, value=1)
s2_rt.client.write_register("MyIngress.switch_id_reg", index=0, value=2)
```

This is the textbook P4 pattern for multi-switch deployments: one
binary, one P4Info, multiple switch identities. The v1.2 register API
makes this a single `write_register` call per switch.

## Run it

In one terminal:

```bash
sudo p4net examples/int_multi_hop/topology.py
```

`setup(net)` installs the L2 forwarding tables on both switches,
pre-seeds static ARP on both hosts, and writes each switch's identity
register. Drop into the `p4net>` shell.

In a second terminal (or `h2 xterm` from the shell):

```bash
sudo ip netns exec h2 python3 examples/int_multi_hop/listener.py --iface h2-eth0
```

From a third terminal:

```bash
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

The listener prints one block per packet. With two switches on the
path, every block has two `hop` lines.

## Sample output

Captured from the v1.4 multi-hop integration test (aligned mode):

```
packet (2 hop(s), final proto 0x0800): 10.0.0.1 -> 10.0.0.2
  hop 1: switch_id=1 ts=800454us aligned=1778513670403185us egress_port=2 queue_depth=0
  hop 2: switch_id=2 ts=699418us aligned=1778513670403875us egress_port=2 queue_depth=0
  latency_s1_to_s2 = 690us
```

`hop 1` is s1 (forwards out port 2 toward s2); `hop 2` is s2
(forwards out port 2 toward h2). Each `ts` is the per-switch
`ingress_global_timestamp` (μs since *that* BMv2 process booted);
`aligned` is wall-clock μs since Unix epoch (the formula in the next
section); `latency_s1_to_s2` is the wall-clock delta between the two
aligned arrival times — real per-hop forwarding latency through BMv2's
userspace pipeline plus the veth pair, typically tens to a couple
thousand microseconds on this rig.

If you run the listener directly with `sudo ip netns exec h2 python3
listener.py --iface h2-eth0` while no topology has populated the
coordination file, it falls back to the v1.3 "unaligned" display, which
prints the raw per-switch `ts` and notes that no alignment was available.

## How cross-switch timestamp alignment works

BMv2's `standard_metadata.ingress_global_timestamp` is **per-process**:
each `simple_switch_grpc` instance's clock starts at zero on boot, so
two switches running side-by-side report timestamps in independent
reference frames. Raw `shim_1.ts` and `shim_2.ts` can't be compared
directly — that's the v1.3 caveat this example used to ship with.

Since v1.4, every `RunningSwitch` exposes a `boot_timestamp_us`
property: wall-clock μs since Unix epoch when its BMv2 process started.
Captured immediately before `subprocess.Popen`, so drift from BMv2's
internal clock zero is bounded by Popen overhead (sub-millisecond on a
typical Linux host).

The alignment formula:

```
wall_clock_us = switch.boot_timestamp_us + shim.ingress_timestamp_us
```

`setup(net)` writes both switches' boot timestamps to a JSON
coordination file at `/tmp/p4net-int-multi-hop-boot-times.json`; the
listener reads it at startup, looks up each shim's hop-index → switch
name, and prints `aligned=...us` next to each raw `ts`. With both
aligned values in hand, the trailing `latency_s1_to_s2 = ...us` line is
just subtraction.

The alignment is good enough for "is per-hop latency in the 1 ms
range or the 100 ms range" — not good enough for nanosecond-precision
research. For real per-link latency in production INT you'd still want
a shared time source (PTP). The drift is on the order of the Popen
syscall (sub-millisecond) plus any kernel-scheduling delay between the
`time.time_ns()` capture and BMv2's actual clock zero.

## What's interesting

- **Per-hop forwarding latency is now observable.** The
  `latency_s1_to_s2` line is the wall-clock delta between hop 1 and
  hop 2; on this rig it ranges from a few hundred microseconds to a
  few milliseconds. Real ASIC switches report tens to hundreds of
  nanoseconds; BMv2's userspace interpreter is two to three orders of
  magnitude slower.
- **Egress ports correspond to the path direction**, not to a fixed
  numbering scheme. s1's port 2 leads to s2; s2's port 2 leads to h2.
  Different topologies will produce different `egress_port` values.
- **`queue_depth` is reliably 0** in this configuration, the same as
  in the single-switch INT example. BMv2's `deq_qdepth` would surface
  non-zero values only under explicit queue configuration with
  saturating offered load — not at one ICMP echo per second.

## Extending to N hops

Replace the two named header instances with a P4 header stack:

```p4
const bit<32> MAX_HOPS = 8;

struct headers {
    ethernet_t ethernet;
    int_shim_t[MAX_HOPS] shims;
    ipv4_t     ipv4;
}
```

Then in the parser:

```p4
state parse_shim {
    pkt.extract(hdr.shims.next);
    transition select(hdr.shims.last.next_proto) {
        ETHERTYPE_IPV4: parse_ipv4;
        ETHERTYPE_INT:  parse_shim;
        default: accept;
    }
}
```

And in ingress, use `hdr.shims.push_front(1)` to allocate a fresh
slot at the head of the stack each hop. The deparser's `emit` on a
header stack walks the stack in order; no explicit per-slot emit
needed.

The listener already handles arbitrary hop counts — it stops decoding
when `next_proto` leaves the INT space, regardless of how many shims
preceded.

## Caveats

- **Two hops only with the current pipeline.** A packet entering a
  third switch on the path would find both shim slots already valid
  and would forward without further annotation. The receiver sees only
  the first two hops; deeper paths require the header-stack rewrite
  above.
- **Alignment drift is sub-millisecond.** `boot_timestamp_us` is
  captured immediately before `Popen`, but BMv2's actual internal
  clock zero is slightly later (after exec, after process init).
  The drift is bounded by Popen + early-init overhead — typically
  well under 1 ms on a typical Linux host, occasionally a couple of
  ms under load. Good enough for "is this in the μs or ms regime",
  not good enough for serious latency research; use PTP for that.
- **Listener relies on a `/tmp/` coordination file.** If multiple
  multi-hop INT topologies are running simultaneously on the same
  host they will trample each other's coordination files. The
  example assumes one topology at a time; production deployments
  would pass boot timestamps to the listener via a more isolated
  channel (a CLI argument, an env var, or a per-topology directory).
- **`queue_depth` is almost always 0.** Same as the single-switch
  example — BMv2's default queueing doesn't surface meaningful values.
- **No checksum recomputation for the inserted shims.** The IPv4
  checksum field covers only the IPv4 header itself; ethernet has no
  cryptographic protection. The shim layer is between them and
  unprotected, which matches how real INT works (the INT spec assumes
  link-layer integrity).
