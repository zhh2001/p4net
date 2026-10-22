---
description: Prepare the Linux tools, install p4net in a virtual environment, and verify the installation.
---

# Installation

Install the Linux tools first, then install p4net in a Python virtual environment. The Python package does not include the P4 compiler or BMv2.

## System requirements

- Linux with kernel 5.4 or newer. CI runs on Ubuntu 22.04 and 24.04.
- Python 3.10 or newer. CI covers Python 3.10–3.13.
- Root privileges to run a network, including creating network namespaces, configuring links and starting switches. Installing the package, defining a topology and compiling P4 programs do not require root.

The commands below use `sudo` for network operations. Run the installation commands as your regular user.

## Install the external tools { #required-external-tools }

On Ubuntu, install the Python environment tools and Linux network utilities:

```bash
sudo apt update
sudo apt install python3-venv python3-pip iproute2 iputils-ping procps git
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

## Install from PyPI

Create and activate a virtual environment, then install p4net:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install p4net
```

pip installs the Python dependencies automatically. Keep this environment active for the remaining commands.

## Install from source

For development, clone the repository and install it in editable mode with the test, lint and type-checking tools:

```bash
git clone https://github.com/zhh2001/p4net.git
cd p4net
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

The documentation uses [CairoSVG](https://cairosvg.org/documentation/) to generate social cards. On Ubuntu, install the system Cairo library with `sudo apt install libcairo2`, then install the documentation tools from the repository root:

```bash
python -m pip install -e '.[dev,docs]'
```

## Verify the installation

Check the installed version, CLI and Python dependencies:

```bash
python -c "import p4net; print(p4net.__version__)"
p4net --help
python -m pip check
```

The first command prints the version you installed. Continue with [Quick Start](quickstart.md) to create `quickstart.py` and its P4 program, then start the network:

```bash
sudo "$(command -v p4net)" quickstart.py
```

`command -v p4net` selects the CLI from the active environment. If you installed the external tools in a custom directory, include that directory in `PATH` when running through `sudo`:

```bash
sudo env "PATH=$PATH" "$(command -v p4net)" quickstart.py
```

## Run tests and build the documentation

With the source installation, run tests from the repository root:

```bash
python -m pytest
```

This selects the default tests. Tests marked as requiring root, p4c or BMv2 are excluded by default. To enable all three groups, install the external tools and run:

```bash
sudo env "PATH=$PATH" "$(command -v python)" -m pytest \
  --run-integration --run-p4c --run-bmv2
```

After installing the documentation tools, build the site with:

```bash
python -m mkdocs build --strict
```

For installation and startup errors, see [Troubleshooting](../troubleshooting.md).
