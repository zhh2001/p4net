---
description: Single-switch INT (in-band network telemetry) demo. The switch inserts a 14-byte shim into every forwarded packet; a raw-socket listener decodes it on the receiver.
---

# INT (in-band telemetry)

Single-switch demo where the P4 pipeline embeds a 14-byte INT
(in-band network telemetry) shim header into every forwarded IPv4
packet. The shim carries the switch identifier, ingress timestamp,
egress port, queue depth, and the original etherType. A raw-socket
listener inside the receiving host's namespace decodes the shim and
prints structured per-packet telemetry.

## What this demonstrates

- **Wire-level header insertion**: the P4 deparser emits a new
  header between Ethernet and IPv4.
- **EtherType swap**: outer etherType becomes `0x88B6` (the INT
  shim identifier) so kernels and packet captures can tell INT
  frames apart.
- **Original etherType preservation**: the shim's `next_proto`
  field carries the original etherType (`0x0800` for IPv4) so
  receivers can recover the inner header chain.
- **Raw-socket decoding**: the user-space listener reads frames via
  `AF_PACKET`, parses the shim by byte offsets, and prints one line
  per frame.

## Topology

`examples/int/topology.py`:

```python
--8<-- "examples/int/topology.py"
```

Two hosts, one switch, IPv4 forwarding programmed via P4Runtime
plus static ARP.

## P4 program

`examples/int/int.p4`:

```p4
--8<-- "examples/int/int.p4"
```

Key points:

- The shim header is declared statically; the deparser emits it
  conditionally on its valid bit.
- The ingress control populates the shim from
  `standard_metadata` after the LPM table has set
  `std.egress_spec`.
- `switch_id` is hardcoded via a `const` because the current
  P4Runtime client doesn't have a register-write API; per-switch
  parameterization would use a default-action table or a recompile.

## The listener

`examples/int/listener.py`:

```python
--8<-- "examples/int/listener.py"
```

The listener opens a raw `AF_PACKET` socket, filters by
`etherType == 0x88B6`, decodes the 14-byte shim by byte offset, and
prints structured output.

## Run it

In one terminal:

```
sudo p4net examples/int/topology.py
```

The `setup(net)` hook installs the LPM entries and pre-seeds static
ARP. You're dropped into the `p4net>` shell.

In a second terminal (or via `h2 xterm` from the shell):

```
sudo ip netns exec h2 python3 examples/int/listener.py --iface h2-eth0
```

In a third terminal, send some traffic:

```
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

The listener prints one line per INT-stamped frame that crossed the
switch:

```
[listener] bound on h2-eth0, waiting for INT frames
[switch=1 ts=164832000ns egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=165834200ns egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=166836100ns egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
```

## Caveats

- **`queue_depth` is almost always 0** with BMv2's default queueing.
  The field is wired in but stays at zero unless the egress queue
  actually backs up — which doesn't happen at this demo's traffic
  level.
- **Single hop only.** Real INT stacks one shim per traversed hop;
  multi-hop is left as an extension exercise.
- **Switch identifier is hardcoded.** Change `SWITCH_ID` in
  `int.p4` to relabel; for multi-switch deployments the obvious
  pattern is a one-row default-action table populated per switch
  at start.

## Variations to try

- Add a second switch with `SWITCH_ID = 2` and chain h1 → s1 →
  s2 → h2. Extend the listener (or the P4 pipeline) to handle a
  shim stack.
- Pipe the listener's output to a file and post-process to compute
  per-flow latency deltas from `ingress_timestamp_ns`.
- Add `delay="50ms"` or `loss_pct=2.0` to one of the h↔s links
  and verify the timestamps and packet counts respond as expected.
