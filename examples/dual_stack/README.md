# dual_stack

Two hosts on a single switch, each carrying both IPv4 and IPv6 addresses.
The pipeline is the same port-swap L2 forwarder used by `quick_start`:
the data plane treats v4 and v6 identically, and static ARP / ND is
pre-seeded so the controller doesn't have to resolve neighbours.

## What it shows

- Per-interface IPv6 enable via `Host.ip6`.
- IPv6 ND seeding via `ip -6 neigh replace`.
- Mixed-traffic ping (IPv4 and IPv6) over the same pipeline.

## Files

- `dual_stack.p4` — port-swap pipeline (verbatim from `tests/fixtures/p4/two_port_swap.p4`).
- `topology.py` — two hosts, one switch, dual-stack addressing, ARP / ND in `setup`.

## Prerequisites

- Linux, Python ≥ 3.10.
- Root privileges (network namespaces and veth creation).
- `p4c` and `simple_switch_grpc` on `PATH`.
- [p4net CLI installed with pipx](https://zhh2001.github.io/p4net/getting-started/installation/#install-cli).

## Running

```sh
sudo p4net examples/dual_stack/topology.py
```

## Things to try in the shell

Run these commands at the `p4net>` prompt.

```text
hosts
pingall
h1 ping h2
h1 ping6 h2
h1 cmd ip -6 addr show
```

`hosts` lists both addresses for each host. `pingall` checks IPv4 connectivity across the topology. Use `h1 ping h2` and `h1 ping6 h2` to test IPv4 and IPv6 between the two hosts. The last command shows the IPv6 addresses configured on `h1`.
