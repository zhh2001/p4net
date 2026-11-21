# Quick start

Two hosts connect to ports 1 and 2 of a BMv2 switch. The P4 program forwards frames between the two ports.

## Prerequisites

- Linux kernel >= 5.4.
- Root privileges to run the example, including creating network namespaces and configuring links.
- `p4c` and `simple_switch_grpc` on `PATH`. The standard p4lang packages on Ubuntu 24.04 install them under `/usr/local/bin`.
- [p4net CLI installed with pipx](https://zhh2001.github.io/p4net/getting-started/installation/#install-cli).

## Run

Run the example from the repository root.

```sh
sudo p4net examples/quick_start/quick_start.py
```

P4Net starts the topology, installs static ARP entries through `setup(net)`, and opens the interactive shell. At the `p4net>` prompt, run `pingall`.

```text
pingall
```

The matrix shows that both directions succeed.

```text
H \ H  h1   h2
h1     -    1
h2     1    -
2/2 succeeded
```

Enter `exit` at the `p4net>` prompt to stop the topology. Add `--no-shell` when starting the example to run it in the foreground until SIGINT.

### As a self-contained script

[Install p4net in a project environment](https://zhh2001.github.io/p4net/getting-started/installation/#project-environments) and activate it. Run the script using that environment's Python.

```sh
sudo "$(command -v python)" examples/quick_start/quick_start.py
```

The standalone script prints the host and switch names, followed by the dictionary returned by `Network.pingall()`.

```
hosts: ['h1', 'h2']
switches: ['s1']
pingall: {('h1', 'h2'): True, ('h2', 'h1'): True}
```

## What the example exercises

- `Topology` construction (hosts, switches, links with auto-assigned ports).
- `Network.start()` — compile P4, create namespaces, wire veths, launch BMv2, push pipeline config.
- Static ARP via `RunningHost.exec`.
- `Network.pingall()` — sequential `ping` between every distinct pair.
- `Network.stop()` (run on context-manager exit) — disconnect P4Runtime, stop BMv2, destroy veths and namespaces.

`quick_start.p4` is a hardcoded swap (port 1 ↔ port 2); no runtime table programming is required, which keeps this example minimal.
