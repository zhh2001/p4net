# Examples

Each subdirectory contains a self-contained P4Net topology. [Install the external tools](https://zhh2001.github.io/p4net/getting-started/installation/#required-external-tools) and [install the CLI with pipx](https://zhh2001.github.io/p4net/getting-started/installation/#install-cli). Then run the command in the example's README from the repository root.

- `quick_start/` — minimal two-host network using a hardcoded port-swap pipeline.
- `l3_forwarding/` — two hosts with runtime-programmed `ipv4_lpm` forwarding and pre-seeded static ARP.
- `cpu_punt/` — punt all dataplane packets to the controller and demonstrate `<switch> packet send` / `<switch> packet listen`.
- `dual_stack/` — two hosts carrying both IPv4 and IPv6 over a port-swap pipeline.
- `asymmetric_link/` — two hosts whose links shape only one direction, demonstrating per-direction `delay_a_to_b` / `delay_b_to_a`.
- `ipv6_lpm/` — IPv6 LPM forwarding programmed at runtime; demonstrates 128-bit field decoding in `<switch> table dump`.
- `int/` — in-band network telemetry: the switch inserts a 14-byte INT shim header (switch ID, ingress timestamp, egress port, queue depth, original etherType) into every forwarded packet; a raw-socket listener decodes it on the receiver.
- `int_multi_hop/` — two-switch INT path. Each switch adds a telemetry header with an ID set through a register.
- `async_concurrent/` — three-switch mesh whose IPv4 forwarding entries are installed concurrently with `AsyncP4RuntimeClient` and `asyncio.gather()`.
