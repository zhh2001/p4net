---
description: Six runnable p4net example topologies, each isolating a single v0.2.0 feature for easy reading and copy-paste.
---

# Examples

p4net ships six runnable example topologies under `examples/` in the
repository. Each one isolates a single capability so the source is
easy to read and copy. Run any of them with:

```
sudo p4net examples/<name>/topology.py
```

(or `sudo python examples/<name>/topology.py` if you don't have the
console script on `PATH`).

## What's bundled

- **[Quick start (port swap)](quick-start.md)** — two hosts on a
  single switch with a hardcoded port-swap pipeline. The smallest
  working topology; no runtime table programming needed.
- **[L3 forwarding](l3-forwarding.md)** — same two hosts, but the
  pipeline routes via an `ipv4_lpm` table that's programmed from
  Python at startup. ARP is pre-seeded.
- **[CPU punt](cpu-punt.md)** — a one-host topology where every
  dataplane packet is punted to the controller via the CPU port.
  Demonstrates `<switch> packet send` and `<switch> packet listen`.
- **[Dual stack](dual-stack.md)** — two hosts each with both IPv4 and
  IPv6 addresses on the same `/24` and `/64`. Shows the per-interface
  IPv6 sysctl gating.
- **[Asymmetric link](asymmetric-link.md)** — two hosts with
  per-direction link delay. Demonstrates `delay_a_to_b` /
  `delay_b_to_a` and the resulting one-way RTT asymmetry.
- **[IPv6 LPM](ipv6-lpm.md)** — two IPv6-only hosts with a 128-bit
  LPM table programmed at runtime. Shows how `<switch> table dump`
  renders IPv6 entries in human form (`fd00::1/128`) instead of raw
  bytes.

## How they fit together

| Example         | Hosts | Switches | IPv4 | IPv6 | Asymmetric | CPU punt | Runtime tables |
| --------------- | ----- | -------- | ---- | ---- | ---------- | -------- | -------------- |
| quick_start     | 2     | 1        | ✓    |      |            |          |                |
| l3_forwarding   | 2     | 1        | ✓    |      |            |          | ✓              |
| cpu_punt        | 1     | 1        | ✓    |      |            | ✓        |                |
| dual_stack      | 2     | 1        | ✓    | ✓    |            |          |                |
| asymmetric_link | 2     | 1        | ✓    |      | ✓          |          |                |
| ipv6_lpm        | 2     | 1        |      | ✓    |            |          | ✓              |

The [Tutorial](../tutorial.md) combines every feature in one
4-host, 2-switch program — useful as a kitchen-sink example, but
each individual feature is easier to understand in isolation here.
