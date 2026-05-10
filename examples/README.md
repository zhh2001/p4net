# Examples

Each subdirectory is a self-contained p4net topology that can be run
either directly (`sudo python <file>.py`) or through the `p4net` console
script (`sudo p4net <file>.py`).

- `quick_start/` — minimal two-host network using a hardcoded port-swap pipeline.
- `l3_forwarding/` — two hosts with runtime-programmed `ipv4_lpm` forwarding and pre-seeded static ARP.
- `cpu_punt/` — punt all dataplane packets to the controller and demonstrate `<switch> packet send` / `<switch> packet listen`.
- `dual_stack/` — two hosts carrying both IPv4 and IPv6 over a port-swap pipeline.
- `asymmetric_link/` — two hosts whose links shape only one direction, demonstrating per-direction `delay_a_to_b` / `delay_b_to_a`.
