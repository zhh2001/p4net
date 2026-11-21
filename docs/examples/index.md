---
description: Nine runnable p4net topologies covering forwarding, per-direction link delay, in-band telemetry, and concurrent switch programming.
---

# Examples

The repository's `examples/` directory contains nine runnable topologies. [Install the external tools](../getting-started/installation.md#required-external-tools) and [install the CLI with pipx](../getting-started/installation.md#install-cli). Then run the quick-start example from the repository root.

```sh
sudo p4net examples/quick_start/quick_start.py
```

Each walkthrough gives the command for its own topology file.

## What's bundled

- **[Quick start (port swap)](quick-start.md)** forwards frames between switch ports 1 and 2.
- **[L3 forwarding](l3-forwarding.md)** installs IPv4 routes from Python and includes a longest-prefix matching exercise.
- **[CPU punt](cpu-punt.md)** shows how to receive PacketIn and send PacketOut through the CPU port.
- **[Dual stack](dual-stack.md)** assigns IPv4 and IPv6 addresses to two hosts and checks connectivity for both.
- **[Asymmetric link](asymmetric-link.md)** shows how different delays in each direction affect ping round-trip time.
- **[IPv6 LPM](ipv6-lpm.md)** installs IPv6 routes and displays them in `table dump` using prefix notation such as `fd00::1/128`.
- **[INT (in-band network telemetry)](int.md)** uses one switch to insert a telemetry header and a receiver to read it.
- **[Multi-hop INT](int-multi-hop.md)** shows the telemetry inserted by each of two switches in series.
- **[Async concurrent](async-concurrent.md)** programs the IPv4 forwarding tables of three switches concurrently.

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
