"""Console-script entry point for the `p4net` command.

Loads a topology Python file, brings up a `Network`, optionally calls
`module.setup(net)`, and either drops the user into the interactive
shell (default) or sleeps until SIGINT (`--no-shell`).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from p4net.network import Network
from p4net.topo import Topology


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="p4net",
        description="Bring up a p4net topology and optionally drop into an interactive shell.",
    )
    p.add_argument(
        "topology_file",
        type=Path,
        help="Path to a .py file defining a module-level `topology: Topology` "
        "(and optionally a `setup(net)` function).",
    )
    p.add_argument(
        "--no-shell",
        action="store_true",
        help="Skip the interactive shell; bring up the network, run setup, "
        "block until SIGINT, then tear down.",
    )
    p.add_argument(
        "--unsafe",
        action="store_true",
        help="Skip Topology.validate() before bring-up.",
    )
    p.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Directory for BMv2 log files (default: a fresh tempdir).",
    )
    p.add_argument(
        "--pcap-dir",
        type=Path,
        default=None,
        help="Directory for per-port BMv2 pcaps (default: pcaps disabled).",
    )
    p.add_argument(
        "--extra-compile-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Extra argument to pass to p4c (repeatable).",
    )
    return p


def _load_topology_module(path: Path) -> ModuleType:
    """Import a Python file by path and return the module."""
    if not path.is_file():
        raise FileNotFoundError(f"topology file not found: {path}")
    spec = importlib.util.spec_from_file_location(f"_p4net_topology_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve_topology(module: ModuleType, path: Path) -> Topology:
    topo = getattr(module, "topology", None)
    if topo is None:
        raise SystemExit(
            f"topology file {path} must define a module-level `topology` "
            "variable of type p4net.topo.Topology"
        )
    if not isinstance(topo, Topology):
        raise SystemExit(
            f"topology file {path}: `topology` must be a p4net.topo.Topology "
            f"instance, got {type(topo).__name__}"
        )
    return topo


def _wait_for_signal() -> None:
    """Block until SIGINT/SIGTERM. Used for --no-shell mode."""
    signal.pause()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    try:
        module = _load_topology_module(ns.topology_file)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ImportError, SyntaxError) as exc:
        print(f"error: could not load {ns.topology_file}: {exc}", file=sys.stderr)
        return 2
    try:
        topology = _resolve_topology(module, ns.topology_file)
    except SystemExit as exc:
        print(f"error: {exc.code}", file=sys.stderr)
        return 2

    setup_fn = getattr(module, "setup", None)
    if setup_fn is not None and not callable(setup_fn):
        print(
            f"error: {ns.topology_file}: `setup` must be callable, got {type(setup_fn).__name__}",
            file=sys.stderr,
        )
        return 2

    network = Network(
        topology,
        log_dir=ns.log_dir,
        pcap_dir=ns.pcap_dir,
        unsafe=ns.unsafe,
        extra_compile_args=tuple(ns.extra_compile_arg),
    )
    with network as net:
        if setup_fn is not None:
            setup_fn(net)
        if ns.no_shell:
            with contextlib.suppress(KeyboardInterrupt):
                _wait_for_signal()
        else:
            # Lazy-import the shell so `p4net --help` works without
            # prompt_toolkit on PATH (it is a runtime dep so this is mostly
            # paranoia, but it keeps `--help` cheap and side-effect-free).
            from p4net.cli.shell import P4NetShell

            with contextlib.suppress(KeyboardInterrupt):
                P4NetShell(net).run()
    return 0


def _entry_point() -> int:
    """Wrapper used by the console script."""
    return main()


if __name__ == "__main__":
    raise SystemExit(main())


# Re-export for the entry point declared in pyproject.toml.
__all__ = ["main"]


# Keep `Any` import alive (used by future extension hooks).
_ = Any
