---
description: Two hosts each carrying both IPv4 and IPv6 addresses on the same /24 and /64. Demonstrates per-interface IPv6 sysctl gating.
---

# Dual stack

Two hosts on a single switch, each carrying both an IPv4 `/24` and an
IPv6 `/64` address. The pipeline is the same port-swap forwarder as
[Quick start](quick-start.md) — L2 swapping treats v4 and v6
identically. What's interesting is the address management.

## What you'll see

`pingall` (IPv4) and `pingall6` (IPv6) both succeed. The hosts'
interfaces carry exactly the addresses we asked for — no `fe80::`
link-local clutter, no SLAAC-derived addresses.

## Topology

`examples/dual_stack/topology.py`:

```python
--8<-- "examples/dual_stack/topology.py"
```

Both `Host.ip` and `Host.ip6` are set. The orchestrator detects this
and runs `enable_ipv6(ns, iface)` (with `accept_ra=0`, `autoconf=0`)
before bringing the interface up, then assigns both addresses.

## P4 program

`examples/dual_stack/dual_stack.p4`:

```p4
--8<-- "examples/dual_stack/dual_stack.p4"
```

The pipeline is L3-agnostic — it only swaps ports. v4 and v6 traffic
take the same path.

## Run it

```
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

p4net> pingall
2/2 succeeded
p4net> pingall6
2/2 succeeded
```

Note: only `fd00::1/64`. No `fe80::` link-local. That's the sysctl
gate doing its job.

## What's interesting

- **`accept_ra=0` and `autoconf=0`** are written along with
  `disable_ipv6=0` so the kernel doesn't silently auto-configure
  additional addresses from a Router Advertisement (which there are
  none of here, but still).
- **Static ND** in `setup(net)` — with `accept_ra` off, IPv6
  neighbor solicitation still works, but doing it for every
  cold-start ping wastes time. Pre-seeded entries keep the latency
  measurements clean.

## Variations to try

- Drop the `ip6` arguments from one host and confirm `pingall6`
  excludes it from the matrix (it filters on `primary_ip6`).
- Set `accept_ra=True` in a manual `enable_ipv6(...)` call and
  observe what addresses appear. (Requires bypassing the orchestrator.)
- Add a `loss_pct=10.0` link parameter and observe that v4 and v6
  pings get the same loss rate (the qdisc is L3-agnostic).
