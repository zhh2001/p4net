# dual_stack

Two hosts with IPv4 and IPv6 addresses share a switch. The switch forwards frames between its two ports, as in `quick_start`. The `setup(net)` function installs static IPv4 and IPv6 neighbor entries on both hosts. Each host uses its peer's configured MAC address from the first ping.

## What it shows

- Per-interface IPv6 enable via `Host.ip6`.
- Static IPv4 and IPv6 neighbor entries via `ip neigh replace` and `ip -6 neigh replace`.
- Mixed-traffic ping (IPv4 and IPv6) over the same pipeline.

## Files

- `dual_stack.p4` — forwards Ethernet frames between switch ports 1 and 2 for both IPv4 and IPv6 traffic.
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
