---
description: The simplest p4net topology — two hosts on a switch with a hardcoded port-swap pipeline, no runtime table programming.
---

# Quick start (port swap)

Two hosts on a single switch with a static port-swap pipeline. No runtime table programming. The "hello world" of p4net.

## Run it

From the repository root, start the topology.

```sh
sudo p4net examples/quick_start/quick_start.py
```

<span id="what-youll-see"></span>At the `p4net>` prompt, use `hosts` to list the interfaces and `pingall` to check connectivity in both directions.

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  -           h1-eth0
h2    10.0.0.2/24  -           h2-eth0

p4net> pingall
H \ H  h1   h2
h1     -    1
h2     1    -
2/2 succeeded
```

When finished, enter `exit` at the `p4net>` prompt to stop the topology.

## Topology

`examples/quick_start/quick_start.py`:

```python
--8<-- "examples/quick_start/quick_start.py"
```

The interesting bits:

- `setup(net)` is the hook the `p4net` console script calls between bring-up and shell. Static ARP is seeded here so the first ICMP doesn't have to resolve.
- The same file works under `python quick_start.py` (the `if __name__ == "__main__"` block) or under `p4net quick_start.py` (the module-level `topology` and `setup`).

## P4 program

`examples/quick_start/quick_start.p4`:

```p4
--8<-- "examples/quick_start/quick_start.p4"
```

The ingress control sets `std.egress_spec` based on `ingress_port` — no tables, no runtime control plane needed.

## What's interesting

- It's the smallest possible working program. If `pingall` succeeds here, the rest of the toolchain (`p4c`, BMv2, namespaces, veth pairs, P4Runtime) is operational.
- The dataplane has no notion of L3 — no IPv4 header parsing, no ARP. Static ARP in `setup(net)` is what makes the L3 ping work.

## Variations to try

- Add a third host on port 3. Without table programming, packets to port 3 hit the implicit drop (since the port-swap covers only 1 ↔ 2).
- Replace the conditional with a single `mark_to_drop(std)` and watch `pingall` produce all `X` cells.
- Add `loss_pct=20.0` to the `add_link()` call for `h1` and `s1`. Compare the loss summaries from `h1 ping h2 10 1` before and after the change.
