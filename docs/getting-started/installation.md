---
description: How to install p4net and the external dependencies it relies on (p4c, BMv2 simple_switch_grpc, graphviz).
---

# Installation

p4net is a thin Python layer over `p4c`, `simple_switch_grpc` (BMv2),
and the Linux kernel's network-namespace and traffic-control
primitives. Installing the Python package is the easy part; getting
the external compiler and dataplane in place is the work.

## System requirements

- **Linux**, with kernel ≥ 5.4. Verified on Ubuntu 22.04 and 24.04;
  other modern distributions should work.
- **Python ≥ 3.10**. CI exercises 3.10–3.13.
- **Root privileges** for the orchestrator. p4net needs `CAP_NET_ADMIN`
  to create network namespaces and veth pairs and to run `tc qdisc`.
  Running with `sudo` is the simplest path; `setcap cap_net_admin+ep`
  on the Python interpreter works but has the usual caveats around
  shebang lookup.

## Required external tools

| Tool                    | Purpose                                                 | Install hint                           |
| ----------------------- | ------------------------------------------------------- | -------------------------------------- |
| `p4c`                   | P4_16 v1model compiler.                                 | `apt install p4lang-p4c` (p4lang OBS), or [build from source](https://github.com/p4lang/p4c). |
| `simple_switch_grpc`    | BMv2 dataplane with the P4Runtime gRPC server.          | [Build from `behavioral-model`](https://github.com/p4lang/behavioral-model) with `--with-pi` and `--with-thrift`. |
| `iproute2` (`ip`, `tc`) | Namespace, link, address, qdisc management.             | Pre-installed on every modern distro.  |
| `graphviz` (`dot`)      | Optional. Required only for `topology graph` rendering. | `apt install graphviz`.                |

Verify each is reachable on `PATH`:

```
$ p4c --version
$ simple_switch_grpc --version
$ ip -V
$ tc -V
$ dot -V        # optional
```

If any of those fail with "command not found", fix the install before
continuing. p4net will surface a clean error at startup if the
binaries are missing, but knowing why ahead of time is faster.

## Install p4net from PyPI

The recommended path is a virtualenv:

```
python3 -m venv .venv
. .venv/bin/activate
pip install p4net
```

Optional extras:

- `pip install 'p4net[dev]'` — testing, linting, type-checking tools
  (`pytest`, `pytest-cov`, `pytest-mock`, `ruff`, `mypy`, `pre-commit`).
- `pip install 'p4net[docs]'` — for building this documentation site
  locally (`mkdocs`, `mkdocs-material`, `mkdocstrings`,
  `mkdocs-static-i18n`).

## Install from source (development)

```
git clone https://github.com/zhh2001/p4net
cd p4net
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

Run the unit tests:

```
pytest -q
```

The integration tests need root and the external binaries; opt in
explicitly:

```
sudo -E env "PATH=$PATH" pytest --run-integration --run-p4c --run-bmv2
```

## Verify

```
$ python -c "import p4net; print(p4net.__version__)"
0.2.0

$ p4net --help
usage: p4net [-h] [--no-shell] [--unsafe] [--log-dir LOG_DIR] ...
```

If `p4net --help` prints `command not found` under `sudo`, your
sudoers `secure_path` strips the venv from `PATH`. Two workarounds:

```
sudo env "PATH=$PATH" p4net <topology.py>           # explicit env
sudo "$(. .venv/bin/activate && which p4net)" ...   # absolute path
```

## Common gotchas

The most common issues — missing namespace tools, sysctl permission
errors, BMv2 startup hangs, gRPC version skew — have entries in the
[Troubleshooting guide](../troubleshooting.md). If you hit something
that's not documented there, please file an issue.
