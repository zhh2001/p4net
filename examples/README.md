# Examples

Each subdirectory is a self-contained p4net topology that can be run
either directly (`sudo python <file>.py`) or through the `p4net` console
script (`sudo p4net <file>.py`).

- `quick_start/` — minimal two-host network using a hardcoded port-swap pipeline.
- `l3_forwarding/` — two hosts with runtime-programmed `ipv4_lpm` forwarding and pre-seeded static ARP.
