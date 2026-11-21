---
description: Read the INT headers from two switches and estimate when a packet reached each hop.
---

# Multi-hop INT

Two switches in series each insert a 14-byte INT shim header into every forwarded IPv4 packet. The receiver reads both headers to see the packet's path. The [single-switch INT example](int.md) introduces the shim format.

## Run it

From the repository root, start the topology in the first terminal.

```sh
sudo p4net examples/int_multi_hop/topology.py
```

Once the `p4net>` prompt appears, open a second terminal in the repository root and start the listener in `h2`.

```sh
sudo ip netns exec h2 python3 examples/int_multi_hop/listener.py --iface h2-eth0 --count 3
```

When the listener prints `boot times loaded`, send three ICMP requests from a third terminal.

```sh
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

The switches add INT headers to the requests. The listener receives all three frames. The receiving host sends no ICMP replies, so `ping` reports `100% packet loss`.

### Sample output

The listener prints two hop lines per packet. This is one of the three packets.

```
packet (2 hop(s), final proto 0x0800): 10.0.0.1 -> 10.0.0.2
  hop 1: switch_id=1 ts=676402us aligned=1795161753919021us egress_port=2 queue_depth=0
  hop 2: switch_id=2 ts=467984us aligned=1795161753918136us egress_port=2 queue_depth=0
  latency_s1_to_s2 = -885us
```

`hop 1` is `s1` and `hop 2` is `s2`. Each `ts` comes from that switch's `ingress_global_timestamp`. The listener estimates both arrival times in Unix microseconds (`aligned`) and prints their difference as `latency_s1_to_s2`. See [how timestamp alignment works](#how-cross-switch-timestamp-alignment-works) for why this estimate can be negative.

In this topology, port 2 leads from `s1` to `s2` and from `s2` to `h2`. At this traffic rate, `queue_depth` is usually zero.

Without a boot-time file, the listener prints the raw `ts` values marked `[unaligned]`.

The listener exits after three frames. Then enter `exit` at the `p4net>` prompt to stop the topology.

## Topology

`examples/int_multi_hop/topology.py`:

```python
--8<-- "examples/int_multi_hop/topology.py"
```

The path is `h1 → s1 → s2 → h2`. The `setup(net)` hook installs the L2 forwarding entries on both switches and configures static ARP on both hosts.

## P4 program

`examples/int_multi_hop/int_multi_hop.p4`:

```p4
--8<-- "examples/int_multi_hop/int_multi_hop.p4"
```

- The program has two slots for telemetry headers, `int_shim_1` and `int_shim_2`. Each switch adds its own header in the first unused slot. After dequeue, egress writes the number of packets still queued into that header's `queue_depth`. Further switches forward the packet with the two existing headers.
- The `next_proto` fields identify the sequence `Ethernet → shim 1 → shim 2 → IPv4`. The IPv4 checksum covers the IPv4 header.
- At startup, the topology assigns a distinct ID to each switch with `write_register("MyIngress.switch_id_reg", index=0, value=N)`.

## The listener

`examples/int_multi_hop/listener.py`:

```python
--8<-- "examples/int_multi_hop/listener.py"
```

The listener checks the outer EtherType and reads one 14-byte header per hop. It follows `next_proto` to identify the next header.

## How cross-switch timestamp alignment works

Each BMv2 process starts its own microsecond counter during initialization. p4net records `RunningSwitch.boot_timestamp_us` in Unix microseconds just before launching the process. The listener uses that timestamp as an estimate of when the counter started. See the [BMv2 timestamp implementation notes](https://github.com/p4lang/behavioral-model/blob/2bdd0b7b/docs/simple_switch.md#bmv2-timestamp-implementation-notes).

```
aligned = boot_timestamp_us + ts
latency_s1_to_s2 = aligned_s2 - aligned_s1
```

The difference estimates the time between the packet arriving at `s1` and arriving at `s2`. The time between the launch timestamp and the start of the BMv2 counter can differ between switches. That difference can make the latency estimate negative, as in the sample above.

`setup(net)` saves `Network.boot_timestamps` to `/tmp/p4net-int-multi-hop-boot-times.json` by default. The listener reads this file at startup.

## Running concurrent topologies

Each copy uses the same node names and default BMv2 gRPC and Thrift ports. Give each copy its own Linux network namespace for the links and ports, its own mount namespace with a private `/run/netns` for `h1` and `h2`, and its own timestamp coordination file.

From the repository root, open one shell per copy.

```bash
sudo unshare --mount --net --propagation private bash
```

In each new shell, run the following commands.

```bash
mkdir -p /run/netns
mount -t tmpfs tmpfs /run/netns
ip link set lo up
int_boot_times=$(mktemp /tmp/p4net-int-boot-times.XXXXXXXX)
trap 'rm -f -- "$int_boot_times"' EXIT
printf 'int_shell_pid=%s int_boot_times=%s\n' "$$" "$int_boot_times"
P4NET_INT_BOOT_TIMES_PATH="$int_boot_times" p4net examples/int_multi_hop/topology.py
```

Once that copy reaches `p4net>`, copy its `int_shell_pid=... int_boot_times=...` line into a second terminal at the repository root. Run the listener in that copy's `h2` namespace.

```bash
sudo nsenter --target "$int_shell_pid" --mount --net -- \
  ip netns exec h2 python3 "$PWD/examples/int_multi_hop/listener.py" \
  --iface h2-eth0 --boot-times "$int_boot_times" --count 3
```

When the listener prints `boot times loaded`, send traffic from the corresponding `p4net>` shell.

```text
h1 ping h2 3 1
```

The listener prints three packets with two aligned hops each. The shell reports `3 sent, 0 received, 100% loss` because the INT-tagged frames reach the listener without producing ICMP replies. When done, type `exit` in `p4net>` and then in the isolated Bash shell. Bash removes that copy's coordination file when it exits.
