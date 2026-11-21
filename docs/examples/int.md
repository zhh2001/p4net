---
description: Insert and read a 14-byte INT header on IPv4 packets forwarded by one switch.
---

# INT (in-band network telemetry)

The switch inserts a 14-byte telemetry header, called a shim, into each forwarded IPv4 packet. The receiver reads the switch ID, ingress timestamp, egress port, queue depth, and original EtherType from this header.

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

- The deparser emits the shim between Ethernet and IPv4 when its valid bit is set. The outer EtherType is `0x88B6` and the shim’s `next_proto` carries the original EtherType (`0x0800` for IPv4).
- The ingress control adds the shim after the LPM table sets `std.egress_spec`. The egress control fills `queue_depth` with the number of packets still in the queue after dequeue.
- `setup(net)` writes switch ID 1 to the `MyIngress.switch_id` register. Changing this value in `setup(net)` leaves the P4 program intact.

## The listener

`examples/int/listener.py`:

```python
--8<-- "examples/int/listener.py"
```

The listener opens a raw `AF_PACKET` socket, filters by `etherType == 0x88B6`, decodes the 14-byte shim by byte offset, and prints structured output.

At the traffic rate used here, the egress queue is typically empty, so `queue_depth` is usually zero.

## Variations to try

- Compare the `ingress_timestamp_us` values to measure the interval between packets arriving at the switch.
- Follow the [two-switch INT example](int-multi-hop.md) to read a telemetry header from each hop.
