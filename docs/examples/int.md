---
description: Single-switch INT (in-band network telemetry) demo. The switch inserts a 14-byte shim into every forwarded packet; a raw-socket listener decodes it on the receiver.
---

# INT (in-band network telemetry)

Single-switch demo where the P4 pipeline embeds a 14-byte INT shim header into every forwarded IPv4 packet. The shim carries the switch identifier, ingress timestamp, egress port, queue depth, and the original etherType. A raw-socket listener inside the receiving host's namespace decodes the shim and prints structured per-packet telemetry.

## Run it

From the repository root, start the topology in the first terminal.

```sh
sudo p4net examples/int/topology.py
```

Once the `p4net>` prompt appears, open a second terminal in the repository root and start the listener in `h2`.

```sh
sudo ip netns exec h2 python3 examples/int/listener.py --iface h2-eth0 --count 3
```

When the listener prints `waiting for INT frames`, send three ICMP requests from a third terminal.

```sh
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

The switch adds an INT header to each request. The listener receives all three frames. The receiving host sends no ICMP replies, so `ping` reports `100% packet loss`.

The listener prints one line per frame.

```
[listener] bound on h2-eth0, waiting for INT frames
[switch=1 ts=745907us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=1750021us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=2754336us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
```

The listener exits after three frames. Then enter `exit` at the `p4net>` prompt to stop the topology.

## Topology

`examples/int/topology.py`:

```python
--8<-- "examples/int/topology.py"
```

The `setup(net)` hook installs the IPv4 forwarding entries via P4Runtime and configures static ARP on both hosts.

## P4 program

`examples/int/int.p4`:

```p4
--8<-- "examples/int/int.p4"
```

<span id="what-this-demonstrates"></span>Key points:

- The deparser emits the shim between Ethernet and IPv4 when its valid bit is set. The outer EtherType is `0x88B6` and the shim’s `next_proto` carries the original EtherType (`0x0800` for IPv4).
- The ingress control populates the shim from `standard_metadata` after the LPM table has set `std.egress_spec`.
- `switch_id` is now register-backed (``register<bit<8>>(1) switch_id;``). The topology's ``setup(net)`` calls ``s1.client.write_register("MyIngress.switch_id", index=0, value=1)``; multi-switch INT deployments can assign distinct identifiers without recompiling.

## The listener

`examples/int/listener.py`:

```python
--8<-- "examples/int/listener.py"
```

The listener opens a raw `AF_PACKET` socket, filters by `etherType == 0x88B6`, decodes the 14-byte shim by byte offset, and prints structured output.

## Caveats

- **`queue_depth` is almost always 0** with BMv2's default queueing. The field is wired in but stays at zero unless the egress queue actually backs up — which doesn't happen at this demo's traffic level.
- Each switch adds one shim in the [two-switch INT example](int-multi-hop.md).
- **Switch identifier is register-backed.** Change the ``write_register("MyIngress.switch_id", index=0, value=N)`` call in the topology's ``setup(net)`` to relabel; multi-switch deployments give each switch a distinct ``N`` with no recompile required.

## Variations to try

- Add a second switch, write its ``switch_id`` register to ``2``, and chain h1 → s1 → s2 → h2. Extend the listener (or the P4 pipeline) to handle a shim stack.
- Pipe the listener's output to a file and post-process to compute per-flow latency deltas from `ingress_timestamp_us`.
- Add `delay="50ms"` or `loss_pct=2.0` to one of the h↔s links and verify the timestamps and packet counts respond as expected.
