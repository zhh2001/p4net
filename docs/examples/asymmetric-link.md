---
description: Two hosts with per-direction link delay. Demonstrates `delay_a_to_b` / `delay_b_to_a` and the resulting asymmetric one-way RTT.
---

# Asymmetric link

Two hosts on a single switch, each link carrying delay in only one
direction. The opposite direction is unshaped, so the resulting
ping RTT is the sum of the two shaped one-ways.

## What you'll see

`h1 ping h2` reports an RTT of approximately 220 ms with sub-ms
jitter — measured RTT in the integration test was
`min/avg/max/mdev = 220.981/221.288/222.048/0.396 ms`.

## Topology

`examples/asymmetric_link/topology.py`:

```python
--8<-- "examples/asymmetric_link/topology.py"
```

The h1↔s1 link has `delay_a_to_b="200ms"` — applied to the `a` side
(h1's namespace). The h2↔s1 link has `delay_b_to_a="20ms"` — applied
to the `b` side (s1's root-namespace veth toward h2).

| Direction         | Path                  | Shaped delay |
| ----------------- | --------------------- | ------------ |
| h1 → s1           | h1's egress veth      | 200 ms       |
| s1 → h2           | s1's egress veth      | 20 ms        |
| h2 → s1           | (none)                | 0 ms         |
| s1 → h1           | (none)                | 0 ms         |

End-to-end one-way h1→h2: 200 + 20 = 220 ms. Reverse h2→h1: 0 ms.
Ping RTT (h1→h2 echo + h2→h1 reply): 220 ms.

## P4 program

`examples/asymmetric_link/asymmetric.p4`:

```p4
--8<-- "examples/asymmetric_link/asymmetric.p4"
```

Identical port-swap pipeline as [Quick start](quick-start.md). The
asymmetry lives entirely in the link impairment, not the dataplane.

## Run it

```
sudo p4net examples/asymmetric_link/topology.py
```

```
p4net> h1 ping h2 5 3
PING 10.0.0.2 (10.0.0.2) 56(84) bytes of data.
64 bytes from 10.0.0.2: icmp_seq=1 ttl=64 time=222 ms
64 bytes from 10.0.0.2: icmp_seq=2 ttl=64 time=221 ms
64 bytes from 10.0.0.2: icmp_seq=3 ttl=64 time=221 ms
64 bytes from 10.0.0.2: icmp_seq=4 ttl=64 time=221 ms
64 bytes from 10.0.0.2: icmp_seq=5 ttl=64 time=221 ms

--- 10.0.0.2 ping statistics ---
5 packets transmitted, 5 received, 0% packet loss, time 4126ms
rtt min/avg/max/mdev = 220.981/221.288/222.048/0.396 ms
```

(That's the literal output captured during phase-12 verification.)

`tc qdisc show` inside h1's namespace confirms which side carries the
netem queue:

```
p4net> h1 cmd tc qdisc show dev h1-eth0
qdisc netem 8001: root ... delay 200ms
```

## What's interesting

- **Direction semantics are fixed by `(a, b)` ordering.**
  `Link(h1, s1, delay_a_to_b="200ms")` shapes h1→s1. If you swap
  the order to `Link(s1, h1, ...)`, the same `delay_a_to_b` would
  shape s1→h1 instead. The `add_link(a, b, ...)` argument order
  is the source of truth.
- **Symmetric and asymmetric on the same parameter is rejected at
  construction.** You can't say `delay="50ms"` plus
  `delay_a_to_b="100ms"`. Pick one or the other for any given
  parameter, then mix freely across parameters
  (e.g. symmetric `bandwidth` with asymmetric `delay`).

## Variations to try

- Set `loss_pct_a_to_b=50.0` instead of delay and observe a 50%
  packet-loss matrix in `pingall`.
- Set `delay_a_to_b="200ms"` and `delay_b_to_a="200ms"` to recover
  symmetric 400 ms RTT.
- Combine asymmetric delay with symmetric bandwidth shaping
  (`bandwidth="1mbit"`) to model a typical residential link.
