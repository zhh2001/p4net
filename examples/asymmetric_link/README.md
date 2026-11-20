# asymmetric_link

Two hosts on a single switch with per-direction link delay applied via ``Link``'s ``*_a_to_b`` / ``*_b_to_a`` fields. The h1↔s1 leg adds 200 ms to the h1→s1 direction; the h2↔s1 leg adds 20 ms to the s1→h2 direction; the reverse directions are unshaped.

The `h1 → h2` path has 220 ms of added delay and the reverse path has none. A ping from either host measures a round trip of about 220 ms.

## What it shows

- Per-direction delay with `delay_a_to_b` and `delay_b_to_a`.
- That direction semantics are anchored to the link's `a` and `b` endpoints (a = first arg to `add_link`, b = second).

## Files

- `asymmetric.p4` — port-swap pipeline (same source as `quick_start`).
- `topology.py` — two hosts, one switch, asymmetric delays.

## Prerequisites

- Linux, Python ≥ 3.10.
- Root privileges (network namespaces and veth creation).
- `p4c` and `simple_switch_grpc` on `PATH`.

## Running

```sh
sudo p4net examples/asymmetric_link/topology.py
```

## Things to try in the shell

```
h1 ping h2 5 3
h2 ping h1 5 3
h1 cmd tc qdisc show
```

Both ping commands report round-trip times of about 220 ms.

The `tc qdisc show` output confirms which side carries the netem queue.
