# l3_forwarding

Two hosts on a `/24` connected by a single BMv2 switch running an IPv4 LPM
forwarding pipeline. Forwarding entries are installed at runtime from
Python via P4Runtime, not baked into the data plane.

## What it shows

- A minimal but realistic LPM forwarding pipeline (`ipv4_lpm.p4`) that
  matches `hdr.ipv4.dstAddr` and sets the egress port.
- Runtime control-plane programming via `s1.client.insert_table_entry(...)`.
- Static ARP seeding so ICMP unicast does not have to resolve neighbours
  at test time.

## Files

- `ipv4_lpm.p4` — P4 program. Same source as `tests/fixtures/p4/simple_routing.p4`.
- `topology.py` — Topology, plus a `setup(net)` function that pre-seeds
  ARP and installs two `/32` LPM entries.

## Prerequisites

- Linux, Python ≥ 3.10.
- Root privileges (network namespaces and veth creation).
- `p4c` and `simple_switch_grpc` on `PATH`.
- `pip install -e '.[dev]'` in a venv.

## Running

Either form drops you into the interactive shell after bringing the
network up and running `setup(net)`:

```
sudo python examples/l3_forwarding/topology.py
sudo p4net examples/l3_forwarding/topology.py
```

If `sudo` strips your venv from `PATH`, invoke through `env`:

```
sudo env "PATH=$PATH" p4net examples/l3_forwarding/topology.py
```

## Things to try in the shell

```
hosts
switches
pingall
s1 table dump MyIngress.ipv4_lpm
s1 counter MyIngress.ingress_pkts
h1 ping h2
h1 cmd ip -br addr
```

Use `Ctrl-D` (or `exit`) to quit. The orchestrator tears the namespaces
and the BMv2 process down on exit.
