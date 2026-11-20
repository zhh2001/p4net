---
description: Connect two dual-stack hosts through one switch and verify IPv4 and IPv6 connectivity.
---

# Dual stack

Two hosts on a single switch, each carrying both an IPv4 `/24` and an IPv6 `/64` address. The pipeline is the same port-swap forwarder as [Quick start](quick-start.md) — L2 swapping treats v4 and v6 identically. What's interesting is the address management.

## What you'll see

`pingall` verifies IPv4 connectivity and `pingall6` verifies IPv6 connectivity. Both commands report `2/2 succeeded`.

## Topology

`examples/dual_stack/topology.py`:

```python
--8<-- "examples/dual_stack/topology.py"
```

Each host sets `Host.ip` and `Host.ip6`. The orchestrator enables IPv6 on the interfaces that receive an IPv6 address before bringing them up.

## P4 program

`examples/dual_stack/dual_stack.p4`:

```p4
--8<-- "examples/dual_stack/dual_stack.p4"
```

The pipeline is L3-agnostic — it only swaps ports. v4 and v6 traffic take the same path.

## Run it

```sh
sudo p4net examples/dual_stack/topology.py
```

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  fd00::1/64   h1-eth0
h2    10.0.0.2/24  fd00::2/64   h2-eth0

p4net> h1 cmd ip -6 addr show dev h1-eth0
3: h1-eth0@if4: <BROADCAST,MULTICAST,UP,LOWER_UP> ...
    inet6 fd00::1/64 scope global
       valid_lft forever preferred_lft forever
    inet6 fe80::200:ff:fe00:1/64 scope link
       valid_lft forever preferred_lft forever

p4net> pingall
2/2 succeeded

p4net> pingall6
2/2 succeeded
```

`fd00::1/64` comes from the topology. In this run, Linux also generated `fe80::200:ff:fe00:1/64` for use on the local link. Link-local address generation depends on the kernel's `addr_gen_mode` setting.

## What's interesting

- p4net sets `accept_ra=0` to ignore router advertisements and `autoconf=0` to disable address configuration from advertised prefixes.
- `setup(net)` adds static IPv4 and IPv6 neighbor entries so the first ping can use the configured MAC addresses.

## Variations to try

- Drop the `ip6` arguments from one host and confirm `pingall6` excludes it from the matrix (it filters on `primary_ip6`).
- Add a `loss_pct=10.0` link parameter and observe that v4 and v6 pings get the same loss rate (the qdisc is L3-agnostic).
