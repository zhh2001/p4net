---
description: Install routes for two IPv4 hosts and test longest-prefix matching in the p4net shell.
---

# L3 forwarding

Two hosts connect to separate ports on a BMv2 switch. At startup, Python installs one route per host in the switch's `ipv4_lpm` table.

## Run it

From the repository root, start the topology.

```sh
sudo p4net examples/l3_forwarding/topology.py
```

At the `p4net>` prompt, check the two `/32` routes installed at startup, then verify connectivity and read the packet counter for egress port 1.

```
p4net> s1 table dump MyIngress.ipv4_lpm
MyIngress.ipv4_lpm (2 entries)
#0
  match:    hdr.ipv4.dstAddr=10.0.0.1/32
  action:   MyIngress.set_egress_port
  params:   port=1

#1
  match:    hdr.ipv4.dstAddr=10.0.0.2/32
  action:   MyIngress.set_egress_port
  params:   port=2

p4net> pingall
H \ H  h1   h2
h1     -    1
h2     1    -
2/2 succeeded

p4net> s1 counter MyIngress.ingress_pkts 1
pkts=2 bytes=196
```

When finished, enter `exit` at the `p4net>` prompt to stop the topology.

## Topology

`examples/l3_forwarding/topology.py`:

```python
--8<-- "examples/l3_forwarding/topology.py"
```

`setup(net)` passes each host's destination prefix and egress port to `s1.client.insert_table_entry()`. The client uses P4Info to encode the match key and action parameter.

## P4 program

`examples/l3_forwarding/ipv4_lpm.p4`:

```p4
--8<-- "examples/l3_forwarding/ipv4_lpm.p4"
```

The ingress control applies `ipv4_lpm` only to IPv4 packets. `setup(net)` supplies static ARP entries so each host already knows the other host's MAC address.

## Variations to try

### Test longest-prefix matching

Each forwarding entry selects one egress port. The two `/32` entries direct traffic for `h1` to port 1 and traffic for `h2` to port 2. With the original topology running, add a broader `/24` drop rule at the `p4net>` prompt.

```text
s1 table add MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.0/24 action: MyIngress.drop
pingall
```

`pingall` still reports `2/2 succeeded` because both `/32` entries take precedence. Delete the route to `h2` and repeat the check.

```text
s1 table del MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.2/32
pingall
```

`pingall` reports `0/2 succeeded`. Requests and replies destined for `h2` now match the `/24` drop rule. Enter `exit` and relaunch the original topology to restore the startup routes.

### Add a third host

Connect `h3` to port 3 with address `10.0.0.3/24` and MAC address `00:00:00:00:00:03`. In `setup(net)`, add a `10.0.0.3/32` route using `MyIngress.set_egress_port` with `port=3`. Add static ARP entries so `h3` and each existing host know each other's MAC address. Launch the extended topology and check that `pingall` reports `6/6 succeeded`.
