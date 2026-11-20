---
description: Multi-hop INT demo. Two switches in series, both running the same P4, each insert their own metadata shim. The receiver decodes the full hop-by-hop chain.
---

# Multi-hop INT

Two switches in series each insert a 14-byte INT shim header into every forwarded IPv4 packet. The receiver reads both headers to see the packet's path. The [single-switch INT example](int.md) introduces the shim format.

## What this demonstrates

- **Hop-by-hop metadata accumulation**: every switch on the path inserts its own shim, so the egress receiver sees one block of metadata per traversed switch.
- **Shim chaining via `next_proto`**: each shim's `next_proto` field names the next header in order. The parser walks `etherType → shim_1.next_proto → shim_2.next_proto → ipv4`. No P4 header stack required for the two-hop case.
- **Per-switch identity from a register**: the same P4 program runs on both switches; per-switch `switch_id` comes from the v1.2 register API via `write_register("MyIngress.switch_id_reg", index=0, value=N)` at start-up.

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

- Two named header instances `int_shim_1` and `int_shim_2` instead of a P4 header stack. Easier to read at two hops; for N hops, see the "Extending" section in the example README.
- Ingress picks the first unfilled shim slot and writes it from `standard_metadata` plus the configured `switch_id`. The `next_proto` chain is re-stitched so the receiver sees `eth → shim_1 → shim_2 → ipv4`.
- The deparser emits every valid header in declaration order.

## The listener

`examples/int_multi_hop/listener.py`:

```python
--8<-- "examples/int_multi_hop/listener.py"
```

The listener walks the shim chain starting from the outer EtherType, parsing one 14-byte shim per hop until `next_proto` leaves the INT space.

## Run it

In one terminal:

```sh
sudo p4net examples/int_multi_hop/topology.py
```

`setup(net)` installs the L2 forwarding tables on both switches, pre-seeds static ARP on both hosts, and writes each switch's identity register. Drop into the `p4net>` shell.

In a second terminal (or `h2 xterm` from the shell):

```sh
sudo ip netns exec h2 python3 examples/int_multi_hop/listener.py --iface h2-eth0
```

From a third terminal:

```sh
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

The listener prints one block per packet, with one `hop` line per traversed switch (two lines per packet in this topology).

## Sample output

One packet captured by the listener with boot timestamps loaded.

```
packet (2 hop(s), final proto 0x0800): 10.0.0.1 -> 10.0.0.2
  hop 1: switch_id=1 ts=676402us aligned=1795161753919021us egress_port=2 queue_depth=0
  hop 2: switch_id=2 ts=467984us aligned=1795161753918136us egress_port=2 queue_depth=0
  latency_s1_to_s2 = -885us
```

`hop 1` is `s1` and `hop 2` is `s2`. Each `ts` comes from that switch's `ingress_global_timestamp`. The listener estimates both arrival times in Unix microseconds (`aligned`) and prints their difference as `latency_s1_to_s2`.

Without a boot-time file, the listener prints the raw `ts` values marked `[unaligned]`.

## How cross-switch timestamp alignment works

Each BMv2 process starts its own microsecond counter during initialization. p4net records `RunningSwitch.boot_timestamp_us` in Unix microseconds just before launching the process. The listener uses that timestamp as an estimate of when the counter started. See the [BMv2 timestamp implementation notes](https://github.com/p4lang/behavioral-model/blob/2bdd0b7b/docs/simple_switch.md#bmv2-timestamp-implementation-notes).

```
aligned = boot_timestamp_us + ts
latency_s1_to_s2 = aligned_s2 - aligned_s1
```

The difference estimates the time between the packet arriving at `s1` and arriving at `s2`. The time between the launch timestamp and the start of the BMv2 counter can differ between switches. That difference can make the latency estimate negative, as in the sample above.

`setup(net)` saves `Network.boot_timestamps` to `/tmp/p4net-int-multi-hop-boot-times.json` by default. Each access to the property returns a new dictionary keyed by switch name. You can edit the copy independently of the running network. The listener loads the file at startup.

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

## What's interesting

- **Egress ports correspond to the path direction.** s1 forwards out port 2 toward s2; s2 forwards out port 2 toward h2. Different topologies produce different numbers.
- **`queue_depth` is reliably 0** at this offered load — BMv2's default queueing doesn't surface non-zero values without explicit configuration and saturation.

## Caveats

- **Two hops only with the current pipeline.** A third switch on the path would find both shim slots full and forward without further annotation. Real deployments use a P4 header stack of MAX_HOPS depth — see the example README for the rewrite recipe.
- **`queue_depth` is almost always 0.** Same as the single-switch example.
- **No checksum recomputation for the inserted shims.** The IPv4 checksum covers only the IPv4 header; the shim layer between Ethernet and IPv4 is unprotected, matching how production INT works (the INT spec assumes link-layer integrity).
