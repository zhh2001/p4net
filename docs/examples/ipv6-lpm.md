---
description: Program IPv6 routes for two hosts and inspect the prefixes and forwarding counters.
---

# IPv6 LPM

Two IPv6 hosts share a switch. P4Runtime installs their routes in the `ipv6_lpm` table, and `table dump` displays prefixes such as `fd00::1/128`.

## Run it

From the repository root, start the topology.

```sh
sudo p4net examples/ipv6_lpm/topology.py
```

Check the two `/128` routes, verify IPv6 connectivity with `pingall6`, and read the packet counter for egress port 2.

```
p4net> hosts
name  primary_ip  primary_ip6  interfaces
h1    -           fd00::1/64   h1-eth0
h2    -           fd00::2/64   h2-eth0

p4net> s1 table dump MyIngress.ipv6_lpm
MyIngress.ipv6_lpm (2 entries)
#0
  match:    hdr.ipv6.dstAddr=fd00::1/128
  action:   MyIngress.set_egress_port
  params:   port=1

#1
  match:    hdr.ipv6.dstAddr=fd00::2/128
  action:   MyIngress.set_egress_port
  params:   port=2

p4net> pingall6
H \ H  h1   h2
h1     -    1
h2     1    -
2/2 succeeded

p4net> s1 counter MyIngress.ipv6_pkts 2
pkts=2 bytes=236
```

When finished, enter `exit` at the `p4net>` prompt to stop the topology.

## Topology

`examples/ipv6_lpm/topology.py`:

```python
--8<-- "examples/ipv6_lpm/topology.py"
```

P4Net enables IPv6 and assigns `fd00::1/64` and `fd00::2/64` before bringing up the host interfaces. `setup(net)` installs static IPv6 neighbor entries and one `/128` route per host.

## P4 program

`examples/ipv6_lpm/ipv6_lpm.p4`:

```p4
--8<-- "examples/ipv6_lpm/ipv6_lpm.p4"
```

The LPM table matches the 128-bit IPv6 destination address. The `set_egress_port` action selects the output port and increments its indirect counter.

## Variations to try

- Replace `fd00::1/128` with `fd00::/64` on port 1 and keep `fd00::2/128` on port 2. The `/128` entry still wins for packets to `h2`. Check that `pingall6` reports `2/2 succeeded`.
- Add a third host on port 3 with address `fd00::3/64` and a unique MAC address. In `setup(net)`, install `fd00::3/128` on port 3 and add static IPv6 neighbor entries between the new host and each existing host. Start the extended topology and check that `pingall6` reports `6/6 succeeded`.
- Use `client.read_counter("MyIngress.ipv6_pkts", index=2)` in Python to read the counter for port 2.
