---
description: Nine runnable p4net topologies covering forwarding, per-direction link delay, in-band telemetry, and concurrent switch programming.
---

# Examples

The repository's `examples/` directory contains nine runnable topologies. [Install the CLI with pipx](../getting-started/installation.md#install-cli), then run the quick-start example from the repository root.

```sh
sudo p4net examples/quick_start/quick_start.py
```

Each walkthrough gives the command for its own topology file.

## What's bundled

- **[Quick start (port swap)](quick-start.md)** — two hosts on a single switch with a hardcoded port-swap pipeline. The smallest working topology; no runtime table programming needed.
- **[L3 forwarding](l3-forwarding.md)** — same two hosts, but the pipeline routes via an `ipv4_lpm` table that's programmed from Python at startup. ARP is pre-seeded.
- **[CPU punt](cpu-punt.md)** — a one-host topology where every dataplane packet is punted to the controller via the CPU port. Demonstrates `<switch> packet send` and `<switch> packet listen`.
- **[Dual stack](dual-stack.md)** — two hosts each with both IPv4 and IPv6 addresses on the same `/24` and `/64`. Shows the per-interface IPv6 sysctl gating.
- **[Asymmetric link](asymmetric-link.md)** — two hosts with different one-way delays, configured with `delay_a_to_b` and `delay_b_to_a`.
- **[IPv6 LPM](ipv6-lpm.md)** — two IPv6-only hosts with a 128-bit LPM table programmed at runtime. Shows how `<switch> table dump` renders IPv6 entries in human form (`fd00::1/128`) instead of raw bytes.
- **[INT (in-band network telemetry)](int.md)** — single-switch INT: the pipeline inserts a 14-byte shim header carrying switch identity, ingress timestamp, egress port, queue depth, and the original etherType. A raw-socket listener decodes the shim on the receiver.
- **[Multi-hop INT](int-multi-hop.md)** — two switches in series each insert their own shim; the listener decodes the full hop-by-hop stack. Production-style telemetry topology; same P4 binary on both switches, identity supplied by the v1.2 register API.
- **[Async concurrent](async-concurrent.md)** — three hosts on a three-switch mesh. `AsyncP4RuntimeClient` populates the switches' IPv4 forwarding tables concurrently.

## How they fit together

| Example         | Hosts | Switches | IPv4 | IPv6 | Asymmetric | CPU punt | Runtime tables | Telemetry |
| --------------- | ----- | -------- | ---- | ---- | ---------- | -------- | -------------- | --------- |
| quick_start     | 2     | 1        | ✓    |      |            |          |                |           |
| l3_forwarding   | 2     | 1        | ✓    |      |            |          | ✓              |           |
| cpu_punt        | 1     | 1        | ✓    |      |            | ✓        |                |           |
| dual_stack      | 2     | 1        | ✓    | ✓    |            |          |                |           |
| asymmetric_link | 2     | 1        | ✓    |      | ✓          |          |                |           |
| ipv6_lpm        | 2     | 1        |      | ✓    |            |          | ✓              |           |
| int             | 2     | 1        | ✓    |      |            |          | ✓              | ✓         |
| int_multi_hop   | 2     | 2        | ✓    |      |            |          | ✓              | ✓ (×2)    |
| async_concurrent | 3    | 3        | ✓    |      |            |          | ✓              |           |

For a four-host, two-switch topology that combines dual-stack forwarding, asymmetric delay, and PacketIn, see the [tutorial](../tutorial.md).
