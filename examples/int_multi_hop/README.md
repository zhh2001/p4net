# Multi-hop INT (in-band network telemetry)

Two switches in series each insert a 14-byte INT shim between the Ethernet and IPv4 headers. The receiver decodes both shims in the order the packet visited the switches. The P4 program has two shim slots and records up to two hops.

The [single-switch INT example](../int/) introduces the same shim format.

## What this demonstrates

- **Shim chaining via `next_proto`.** Each shim's `next_proto` field identifies the next header. The parser and listener follow the chain `Ethernet → shim 1 → shim 2 → IPv4`.
- **Per-switch identity from a register.** Both switches run the same P4 program. `setup(net)` writes a distinct `switch_id` to each switch's register.

## Topology

```
  h1 (10.0.0.1/24, 00:00:00:00:00:01)
   |
   | port 1
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

Four nodes, three links, no branching. Same P4 binary loaded on both switches; per-switch identity supplied by the register write.

## The P4 pipeline

`int_multi_hop.p4`:

- **Three headers**: `ethernet_t`, `int_shim_t` (two named instances — `int_shim_1` and `int_shim_2`), and `ipv4_t`.
- **Parser** follows `etherType` and `next_proto` to extract up to two shims before parsing IPv4.
- **Ingress** runs `l2_forward.apply()` to select the egress port. It fills the first available shim with `standard_metadata` and the configured `switch_id`, then updates the header chain. With both slots filled, the packet is forwarded with the two existing shims.
- **Deparser** emits every valid header in declaration order. Invalid shim headers are skipped entirely; the wire bytes never contain unused 14-byte gaps.

## Per-switch parameterization

The P4 program runs unchanged on both switches. Per-switch identity comes from a register written at start-up:

```python
s1_rt.client.write_register("MyIngress.switch_id_reg", index=0, value=1)
s2_rt.client.write_register("MyIngress.switch_id_reg", index=0, value=2)
```

## Run it

In one terminal:

```bash
sudo p4net examples/int_multi_hop/topology.py
```

`setup(net)` installs the L2 forwarding tables on both switches, pre-seeds static ARP on both hosts, and writes each switch's identity register. Drop into the `p4net>` shell.

In a second terminal (or `h2 xterm` from the shell):

```bash
sudo ip netns exec h2 python3 examples/int_multi_hop/listener.py --iface h2-eth0
```

From a third terminal:

```bash
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

The listener prints one block per packet. With two switches on the path, every block has two `hop` lines.

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

- **Egress ports correspond to the path direction**, not to a fixed numbering scheme. s1's port 2 leads to s2; s2's port 2 leads to h2. Different topologies will produce different `egress_port` values.
- **`queue_depth` is reliably 0** in this configuration, the same as in the single-switch INT example. BMv2's `deq_qdepth` would surface non-zero values only under explicit queue configuration with saturating offered load — not at one ICMP echo per second.

## Extending to more hops

Replace the two named shim headers with `int_shim_t[MAX_HOPS] shims`. Choose `MAX_HOPS` at compile time and append each new shim after the existing ones to preserve path order. See the P4 language specification on header stacks.

1. In the parser, set `hop_count` to zero and clear an `added_shim` flag in packet metadata. Before each INT extraction, check that `hop_count` is below `MAX_HOPS`. Extract `hdr.shims.next`, increment `hop_count`, and follow `hdr.shims.last.next_proto`. Continue for `ETHERTYPE_INT`, parse IPv4 for `ETHERTYPE_IPV4`, and reject other values in the shim chain.
2. In ingress, after selecting the egress port, append at index `hop_count` if a slot is available. Mark the new shim valid, set `added_shim`, and fill in the switch ID, ingress timestamp, and egress port. Set `reserved` to zero and `next_proto` to `ETHERTYPE_IPV4`. For the first shim, set the Ethernet EtherType to `ETHERTYPE_INT`. For later shims, set the previous shim's `next_proto` to `ETHERTYPE_INT`. At capacity, forward with the existing shims.
3. In egress, when `added_shim` is set, fill the new shim's `queue_depth` at index `hop_count` from `standard_metadata.deq_qdepth`, which BMv2 provides after dequeuing. Emit Ethernet, the shim stack, and IPv4 in that order.
4. Assign a distinct `switch_id` to each switch and provide its boot timestamp to the receiver. The existing listener decodes longer shim chains through `next_proto` and aligns timestamps for the first two hop positions. To align longer or reordered paths, look up each boot timestamp by the decoded `switch_id`.

## Caveats

- **`queue_depth` is almost always 0.** Same as the single-switch example — BMv2's default queueing doesn't surface meaningful values.
- **No checksum recomputation for the inserted shims.** The IPv4 checksum field covers only the IPv4 header itself; ethernet has no cryptographic protection. The shim layer is between them and unprotected, which matches how real INT works (the INT spec assumes link-layer integrity).
