---
description: Two hosts with different one-way delays. See how `delay_a_to_b` and `delay_b_to_a` contribute to ping round-trip time.
---

# Asymmetric link

Two hosts share one switch. Each link adds delay in only one direction.

## Run it

From the repository root, start the topology.

```sh
sudo p4net examples/asymmetric_link/topology.py
```

`h1 ping h2 5 3` and `h2 ping h1 5 3` both report round-trip times (RTT) around 220 ms.

```
p4net> h1 ping h2 5 3
PING h2 (10.0.0.2)
seq=1  time=222 ms
seq=2  time=221 ms
seq=3  time=221 ms
seq=4  time=221 ms
seq=5  time=221 ms
5 sent, 5 received, 0% loss
```

`tc qdisc show` inside h1's namespace confirms which side carries the netem queue:

```
p4net> h1 cmd tc qdisc show dev h1-eth0
qdisc netem 8001: root ... delay 200ms
```

When finished, enter `exit` at the `p4net>` prompt to stop the topology.

## Topology

`examples/asymmetric_link/topology.py`:

```python
--8<-- "examples/asymmetric_link/topology.py"
```

The `h1`–`s1` link uses `delay_a_to_b="200ms"` on egress from `h1`. The `h2`–`s1` link uses `delay_b_to_a="20ms"` on egress from `s1` toward `h2`.

| Direction | Added delay |
| --------- | ----------- |
| h1 → s1   | 200 ms      |
| s1 → h2   | 20 ms       |
| h2 → s1   | 0 ms        |
| s1 → h1   | 0 ms        |

Traffic from `h1` to `h2` gets 220 ms of added delay. The reverse path has no configured delay. A ping measures the request and reply together, so it includes both paths regardless of which host sends the request.

## P4 program

`examples/asymmetric_link/asymmetric.p4`:

```p4
--8<-- "examples/asymmetric_link/asymmetric.p4"
```

Identical port-swap pipeline as [Quick start](quick-start.md). The asymmetry lives entirely in the link impairment, not the dataplane.

## What's interesting

- **Direction semantics are fixed by `(a, b)` ordering.** `Link(h1, s1, delay_a_to_b="200ms")` shapes h1→s1. If you swap the order to `Link(s1, h1, ...)`, the same `delay_a_to_b` would shape s1→h1 instead. The `add_link(a, b, ...)` argument order is the source of truth.
- **Symmetric and asymmetric on the same parameter is rejected at construction.** You can't say `delay="50ms"` plus `delay_a_to_b="100ms"`. Pick one or the other for any given parameter, then mix freely across parameters (e.g. symmetric `bandwidth` with asymmetric `delay`).

## Variations to try

- On the `h1`–`s1` link, replace `delay_a_to_b="200ms"` with `loss_pct_a_to_b=50.0` to give packets a 50% chance of being dropped in that direction. Run `h1 ping h2 10 1` to see the measured loss across ten probes. In `pingall 10 1`, each matrix cell shows whether that host pair got at least one reply.
- Add `delay_b_to_a="200ms"` to the `h1`–`s1` link and remove `delay_b_to_a="20ms"` from the `h2`–`s1` link. This adds 200 ms in each direction, for a ping RTT of about 400 ms.
- Combine asymmetric delay with symmetric bandwidth shaping (`bandwidth="1mbit"`) to model a typical residential link.
