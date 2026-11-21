# INT (in-band network telemetry)

In-band telemetry carries a switch's observations in the packet. In this example, one switch adds a 14-byte INT shim with its ID, ingress timestamp, egress port, and queue depth. The receiver reads the shim.

## What this demonstrates

- **Wire-level header insertion** — the P4 pipeline declares a new header, populates it from `standard_metadata`, and the deparser emits it between Ethernet and IPv4 on the wire.
- **EtherType swap** — outer Ethernet's `etherType` is rewritten to a custom value (`0x88B6`) so existing kernels and tcpdump filters can cleanly identify INT-stamped frames.
- **Original etherType preservation** — the shim's `next_proto` field carries the original etherType (`0x0800` for IPv4) so receivers can recover the inner header chain.
- **Raw-socket decoding** — a user-space listener inside the receiving host namespace reads frames via `AF_PACKET`, parses the shim, and prints structured per-packet telemetry.

## Topology

```
   h1                s1                h2
 (10.0.0.1/24) --- int.p4 --- (10.0.0.2/24)
               port 1   port 2
```

One switch, two hosts. Every packet that the switch's `ipv4_lpm` table forwards has the shim inserted before egress.

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

The parser extracts the IPv4 header from incoming IPv4 packets.

1. Ingress applies the `ipv4_lpm` table to set `std.egress_spec`.
2. When forwarding a packet, ingress adds the shim and sets the outer EtherType to `0x88B6`.
3. After the packet leaves the switch's egress queue, the egress control records the number of packets still queued as `queue_depth`.

The deparser emits Ethernet → shim → IPv4 in that order. `setup(net)` sets the switch ID through the `MyIngress.switch_id` register.

## The listener

`listener.py` runs inside the receiving host's namespace. It opens a raw AF_PACKET socket, filters by `etherType == 0x88B6`, and unpacks the 14-byte shim. Key bytes:

| Bytes | Field |
| ----- | ----- |
| `0`   | `switch_id` |
| `1..6` | `ingress_timestamp_us` (48-bit big-endian timestamp in microseconds) |
| `7..8` | `egress_port` |
| `9..10` | `queue_depth` |
| `11..12` | `next_proto` |
| `13`  | `reserved` |

When `next_proto == 0x0800`, the listener also peeks 12 bytes into the inner IPv4 header to print the flow's source and destination addresses.

## Run it

From the repository root, start the topology in the first terminal.

```bash
sudo p4net examples/int/topology.py
```

`setup(net)` installs the LPM entries and pre-seeds static ARP before the shell opens.

Once the `p4net>` prompt appears, start the listener from a second terminal in the repository root.

```bash
sudo ip netns exec h2 python3 examples/int/listener.py --iface h2-eth0 --count 3
```

When the listener prints `waiting for INT frames`, send three ICMP requests from a third terminal.

```bash
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

The listener prints one line per ICMP request. The requests reach `h2` as INT frames, so `ping` reports `100% packet loss`.

```
[listener] bound on h2-eth0, waiting for INT frames
[switch=1 ts=745907us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=1750021us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=2754336us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
```

At this send rate, the egress queue is usually empty after each dequeue, so the listener typically prints `queue=0`.

The listener exits after three frames. Enter `exit` at the `p4net>` prompt to stop the topology.

## Variations to try

- Change the `switch_id` register write in `setup(net)` from `value=1` to something else and confirm the listener reports the new value without any P4 recompile.
- Run the ping command with `-i 1` and `-i 0.2` in separate trials. Subtract consecutive `ts` values (`ingress_timestamp_us`) in each trial to measure the interval between packet arrivals at `s1` in microseconds.
- Set `delay_a_to_b="100ms"` on the `h1` to `s1` link, restart the topology, and repeat with the same send interval. The fixed delay shifts arrivals by about the same amount, so the gaps between them stay close to the send interval.

## Next steps

Run the [two-switch INT example](../int_multi_hop/README.md) to see both switches add a shim to the same packet. The listener decodes both records in path order.
