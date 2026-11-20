---
description: Install the Linux tools and the p4net CLI, or set up p4net in a Python project.
---

# Installation

Install the Linux tools first. For the CLI tutorials, install p4net globally with pipx so that `sudo p4net` works. The Python package does not include the P4 compiler or BMv2.

## System requirements

- Linux with kernel 5.4 or newer.
- Python 3.10 or newer.
- Root privileges to run a network, including creating network namespaces, configuring links and starting switches. Defining a topology and compiling P4 programs can use your regular account.

## Install the external tools { #required-external-tools }

On Ubuntu, install the Python environment tools and Linux network utilities:

```bash
sudo apt update
sudo apt install python3-venv python3-pip iproute2 iputils-ping procps
```

Install the P4 compiler and BMv2 using their upstream instructions:

- **p4c**: follow the [packaged installation instructions](https://github.com/p4lang/p4c#installing-packaged-versions-of-p4c), or build it from source. The installation must include the BMv2 backend and the `core.p4` and `v1model.p4` headers.
- **BMv2**: follow the [`simple_switch_grpc` build instructions](https://github.com/p4lang/behavioral-model/tree/main/targets/simple_switch_grpc#dependencies-and-build-steps). This target requires PI with Protobuf/gRPC support and BMv2 configured with `--with-pi`.

p4net uses these commands:

| Command | Purpose | Provided by |
| --- | --- | --- |
| `p4c` | Compile P4_16 v1model programs | p4c with the BMv2 backend |
| `simple_switch_grpc` | Run switches and serve P4Runtime | BMv2 |
| `ip`, `tc` | Manage namespaces, links, addresses and traffic control | `iproute2` |
| `ping` | Check connectivity from hosts | `iputils-ping` |
| `sysctl` | Configure network settings in namespaces | `procps` |

Check that the commands are on `PATH`:

```bash
p4c --version
simple_switch_grpc --version
ip -V
tc -V
ping -V
sysctl --version
```

Two features need additional tools:

- Rendering topology diagrams as SVG, PNG or PDF requires Graphviz (`sudo apt install graphviz`). Exporting DOT files does not require Graphviz.
- Reading, writing or resetting registers requires `simple_switch_CLI` and BMv2's Thrift service. Keep Thrift support enabled when building BMv2 if you need these operations.

## Install the CLI with pipx { #install-cli }

On Ubuntu, install pipx from the system packages. For other distributions, follow the [pipx installation guide](https://pipx.pypa.io/stable/how-to/install-pipx.html) and make sure `sudo pipx --version` runs.

```bash
sudo apt install pipx
sudo pipx --version
```

With pipx 1.6.0 or newer, pass `--global` after `install`.

```bash
sudo pipx install --global p4net
```

With older pipx versions, such as pipx 1.4.3 in Ubuntu 24.04's default repositories, specify the same [global installation directories](https://pipx.pypa.io/stable/how-to/configure-paths.html#install-for-all-users) explicitly.

```bash
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin \
  PIPX_MAN_DIR=/usr/local/share/man pipx install p4net
```

## Verify the installation

Check that `sudo` can find and run the CLI.

```bash
sudo p4net --help
```

pipx places the command in `/usr/local/bin` by default and keeps p4net and its Python dependencies in an isolated environment under `/opt/pipx`. Once the help appears, continue with [Quick Start](quickstart.md).

## Install in a project environment { #project-environments }

To import p4net in your own Python project or use additional Python dependencies in a topology, install it in the project's virtual environment.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install p4net
```

With this environment active, use its p4net entry point when following a CLI example.

```bash
sudo "$(command -v p4net)" quickstart.py
```

`command -v` selects the CLI from the active environment before `sudo` switches users. The same CLI is available through `sudo "$(command -v python)" -m p4net quickstart.py`.

For development setup, tests and documentation builds, see [Contributing](../dev/contributing.md#development-setup).

For installation and startup errors, see [Troubleshooting](../troubleshooting.md).
