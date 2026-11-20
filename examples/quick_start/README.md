# Quick start

A minimal end-to-end demonstration of p4net: two host namespaces, one
BMv2 switch running a port-2-port swap, and `pingall` returning
`{('h1','h2'): True, ('h2','h1'): True}` once the data plane is up.

## Prerequisites

- Linux kernel >= 5.4.
- Root or `CAP_NET_ADMIN` (needed to create network namespaces and
  manage veth pairs).
- `p4c` and `simple_switch_grpc` on `PATH`. The standard p4lang
  packages on Ubuntu 24.04 install them under `/usr/local/bin`.
- [p4net CLI installed with pipx](https://zhh2001.github.io/p4net/getting-started/installation/#install-cli).

## Run

Run the example from the repository root.

```sh
sudo p4net examples/quick_start/quick_start.py
```

The console script imports the file as a Python module, looks up the
module-level `topology: Topology`, brings up the network, runs
`setup(net)` (here: pre-seed static ARP), and drops you into the
interactive shell. Add `--no-shell` to skip the shell and just hold the
network up until SIGINT.

### As a self-contained script

[Install p4net in a project environment](https://zhh2001.github.io/p4net/getting-started/installation/#project-environments) and activate it. Run the script using that environment's Python.

```sh
sudo "$(command -v python)" examples/quick_start/quick_start.py
```

The standalone script prints the host names, the switch name, and the ping results.

```
hosts: ['h1', 'h2']
switches: ['s1']
pingall: {('h1', 'h2'): True, ('h2', 'h1'): True}
```

## What the example exercises

- `Topology` construction (hosts, switches, links with auto-assigned ports).
- `Network.start()` — compile P4, create namespaces, wire veths, launch
  BMv2, push pipeline config.
- Static ARP via `RunningHost.exec`.
- `Network.pingall()` — sequential `ping` between every distinct pair.
- `Network.stop()` (run on context-manager exit) — disconnect P4Runtime,
  stop BMv2, destroy veths and namespaces.

`quick_start.p4` is a hardcoded swap (port 1 ↔ port 2); no runtime
table programming is required, which keeps this example minimal.
