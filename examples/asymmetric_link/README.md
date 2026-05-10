# asymmetric_link

Two hosts on a single switch with per-direction link delay applied via
``Link``'s ``*_a_to_b`` / ``*_b_to_a`` fields. The h1↔s1 leg adds 200 ms
to the h1→s1 direction; the h2↔s1 leg adds 20 ms to the s1→h2 direction;
the reverse directions are unshaped.

Resulting one-way delays:

- h1 → h2: ~200 ms (at h1) + ~20 ms (at s1) ≈ 220 ms.
- h2 → h1: ~0 ms.
- ping RTT from h1 to h2: ~220 ms.

## What it shows

- Per-direction `delay_a_to_b` / `delay_b_to_a` overriding the symmetric
  default.
- That direction semantics are anchored to the link's `a` and `b`
  endpoints (a = first arg to `add_link`, b = second).

## Files

- `asymmetric.p4` — port-swap pipeline (same source as `quick_start`).
- `topology.py` — two hosts, one switch, asymmetric delays.

## Prerequisites

- Linux, Python ≥ 3.10.
- Root privileges (network namespaces and veth creation).
- `p4c` and `simple_switch_grpc` on `PATH`.

## Running

```
sudo p4net examples/asymmetric_link/topology.py
```

## Things to try in the shell

```
h1 ping h2 5 3        # five pings with a 3-second per-reply timeout;
                      # min/avg/max RTT around 220 ms is expected
h1 cmd tc qdisc show
```

The `tc qdisc show` output confirms which side carries the netem queue.
