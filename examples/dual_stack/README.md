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
- `pip install -e '.[dev]'` in a venv.

## Running

```
sudo python examples/dual_stack/topology.py
sudo p4net examples/dual_stack/topology.py
```

If `sudo` strips your venv from `PATH`, invoke through `env`:

```
sudo env "PATH=$PATH" p4net examples/dual_stack/topology.py
```

## Things to try in the shell

```
hosts                    # both v4 and v6 columns now populated
pingall                  # IPv4 mesh (v6 is not pinged automatically)
h1 ping h2               # IPv4
h1 ping6 h2              # IPv6
h1 cmd ip -6 addr show
```
