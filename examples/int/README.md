# INT (in-band network telemetry)

In-band telemetry is the technique where a forwarding device embeds its
own state — switch identity, ingress timestamp, egress port, queue depth
— **inside** the packet's wire-level bytes, so the receiver can read the
device's perspective without a separate control-plane channel. Real INT
deployments stack one shim per hop along a path; this single-switch demo
keeps the shim depth at one to make the encoding and decoding easy to
read.

## What this demonstrates

- **Wire-level header insertion** — the P4 pipeline declares a new
  header, populates it from `standard_metadata`, and the deparser emits
  it between Ethernet and IPv4 on the wire.
- **EtherType swap** — outer Ethernet's `etherType` is rewritten to a
  custom value (`0x88B6`) so existing kernels and tcpdump filters can
  cleanly identify INT-stamped frames.
- **Original etherType preservation** — the shim's `next_proto` field
  carries the original etherType (`0x0800` for IPv4) so receivers can
  recover the inner header chain.
- **Raw-socket decoding** — a user-space listener inside the receiving
  host namespace reads frames via `AF_PACKET`, parses the shim, and
  prints structured per-packet telemetry.

## Topology

```
   h1                s1                h2
 (10.0.0.1/24) --- int.p4 --- (10.0.0.2/24)
                port 1   port 2
```

One switch, two hosts. Every packet that the switch's `ipv4_lpm` table
forwards has the shim inserted before egress.

## The P4 pipeline

`int.p4` declares three headers:

```p4
header ethernet_t { ... }            // 14 B, standard
header int_shim_t {                  // 14 B, novel
    bit<8>  switch_id;
    bit<48> ingress_timestamp_us;
    bit<16> egress_port;
    bit<16> queue_depth;
    bit<16> next_proto;
    bit<8>  reserved;
}
header ipv4_t { ... }                // 20 B, standard
```

The parser handles only inbound IPv4 (this is an ingress switch in the
demo; it never sees INT-stamped frames at its own input). The ingress
control:

1. Applies the `ipv4_lpm` table to set `std.egress_spec`.
2. If forwarding succeeded, populates the shim from
   `standard_metadata` and rewrites the outer `etherType` to `0x88B6`.

The deparser emits Ethernet → shim → IPv4 in that order.

## The listener

`listener.py` runs inside the receiving host's namespace. It opens a raw
AF_PACKET socket, filters by `etherType == 0x88B6`, and unpacks the
14-byte shim. Key bytes:

| Bytes | Field |
| ----- | ----- |
| `0`   | `switch_id` |
| `1..6` | `ingress_timestamp_us` (48-bit big-endian; BMv2 reports microseconds) |
| `7..8` | `egress_port` |
| `9..10` | `queue_depth` |
| `11..12` | `next_proto` |
| `13`  | `reserved` |

When `next_proto == 0x0800`, the listener also peeks 12 bytes into the
inner IPv4 header to print the flow's source and destination addresses.

## Run it

In one terminal:

```bash
sudo p4net examples/int/topology.py
```

`setup(net)` installs the LPM entries and pre-seeds static ARP. Drop
into the shell.

In a second terminal (or from the p4net shell via `h2 xterm`):

```bash
sudo ip netns exec h2 python3 examples/int/listener.py --iface h2-eth0
```

In a third terminal:

```bash
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

The listener prints one line per ICMP echo request that crossed the
switch:

```
[listener] bound on h2-eth0, waiting for INT frames
[switch=1 ts=745907us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=1750021us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=2754336us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
```

(`queue=0` is expected — see caveats.)

## Caveats

- **`queue_depth` is almost always 0** with BMv2's default queueing
  configuration. The `standard_metadata.deq_qdepth` field is populated
  but stays at 0 unless the switch's egress queue actually backs up,
  which doesn't happen at this demo's traffic level. Real INT
  deployments care about queue depth; this demo just shows where it
  plumbs in.
- **Single hop only.** Real INT stacks one shim per traversed switch
  and lets the egress switch strip them all. Multi-hop is left as an
  extension exercise.
- **Switch identifier is now register-backed.** Since p4net 1.2.0 the
  pipeline declares ``register<bit<8>>(1) switch_id;`` and the topology's
  ``setup(net)`` writes it via
  ``s1.client.write_register("MyIngress.switch_id", index=0, value=1)``.
  In a multi-switch INT deployment, distinguish switches without
  recompiling:

  ```python
  s1.client.write_register("MyIngress.switch_id", index=0, value=1)
  s2.client.write_register("MyIngress.switch_id", index=0, value=2)
  ```

## Compatibility

The on-the-wire shim layout is **unchanged** from v1.1.0; only the
P4-level field name was renamed from `ingress_timestamp_ns` to
`ingress_timestamp_us` in v1.2.0 to correctly reflect BMv2's
microsecond-resolution `standard_metadata.ingress_global_timestamp`.
A v1.1.0 packet capture decodes identically against the v1.2.0
listener.

## Variations to try

- Change the `switch_id` register write in `setup(net)` from `value=1`
  to something else and confirm the listener reports the new value
  without any P4 recompile.
- Add a second switch, write its `switch_id` register to `2`, and chain
  h1 → s1 → s2 → h2. Extend the listener (or the P4 pipeline) to
  recognize stacked shims. (Already done in
  [`examples/int_multi_hop/`](../int_multi_hop/) — see the cross-link
  below.)
- Pipe the listener's output to a file and post-process offline to
  compute per-flow latency from `ingress_timestamp_us` deltas across
  consecutive packets.
- Add a per-link `loss_pct` or `delay` to the h1↔s1 link and watch
  the timestamps reflect the delay (subject to `clock_realtime`
  granularity inside BMv2).

## Next steps

For a more realistic deployment pattern with metadata accumulating
across multiple switches, see
[`examples/int_multi_hop/`](../int_multi_hop/) — two switches in
series, both running the same P4 program, each writing its own
`switch_id` register to a distinct value.
