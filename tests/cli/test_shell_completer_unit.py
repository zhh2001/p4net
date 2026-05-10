"""Unit tests for `p4net.cli.completers` and the `P4NetShell` constructor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from p4net.cli import CommandDispatcher, P4NetShell
from p4net.cli.completers import build_network_completer
from p4net.cli.formatting import bold, red, render_pingall_matrix
from p4net.network import Network


def _network_with(hosts: list[str], switches: list[str]) -> MagicMock:
    n = MagicMock(spec=Network)
    n.hosts = {h: MagicMock(name=f"host-{h}") for h in hosts}
    n.switches = {s: MagicMock(name=f"sw-{s}") for s in switches}
    n.is_running = True
    n.log_dir = Path("/tmp")
    n.host = lambda name: n.hosts[name]
    n.switch = lambda name: n.switches[name]
    return n


def test_completer_lists_top_level_commands_and_node_names() -> None:
    n = _network_with(["h1", "h2"], ["s1"])
    d = CommandDispatcher(n)
    completer = build_network_completer(d)
    options = completer.options  # NestedCompleter exposes its dict
    assert "help" in options
    assert "pingall" in options
    assert "h1" in options
    assert "h2" in options
    assert "s1" in options


def test_completer_host_subverbs() -> None:
    n = _network_with(["h1"], [])
    d = CommandDispatcher(n)
    completer = build_network_completer(d)
    h1 = completer.options["h1"]
    assert h1 is not None
    sub_keys = set(h1.options.keys())
    assert sub_keys == {"ping", "ping6", "cmd", "ifconfig", "xterm"}


def test_completer_switch_subverbs() -> None:
    n = _network_with([], ["s1"])
    d = CommandDispatcher(n)
    completer = build_network_completer(d)
    s1 = completer.options["s1"]
    assert s1 is not None
    sub_keys = set(s1.options.keys())
    # commit-3 verbs surface here; phase-11 added "packet".
    assert sub_keys == {"log", "table", "counter", "mcast", "packet"}


def test_shell_constructor_resolves_history_path(tmp_path: Path) -> None:
    n = _network_with(["h1"], [])
    history = tmp_path / "subdir" / ".history"
    shell = P4NetShell(n, history_file=history, prompt="x> ")
    # Constructor must NOT touch the filesystem; only `run()` does.
    assert isinstance(shell.dispatcher, CommandDispatcher)


def test_shell_dispatcher_color_is_on() -> None:
    n = _network_with(["h1"], [])
    shell = P4NetShell(n)
    assert shell.dispatcher.color is True


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def test_bold_color_off() -> None:
    assert bold("hi", color=False) == "hi"


def test_bold_color_on_wraps_with_ansi() -> None:
    out = bold("hi", color=True)
    assert "\x1b[1m" in out
    assert "\x1b[0m" in out
    assert "hi" in out


def test_red_color_off() -> None:
    assert red("hi", color=False) == "hi"


def test_red_color_on_wraps() -> None:
    out = red("hi", color=True)
    assert "\x1b[31m" in out


def test_render_pingall_no_hosts() -> None:
    assert render_pingall_matrix([], {}) == "(no hosts to ping)"


def test_render_pingall_with_unknown_pair_renders_question_mark() -> None:
    out = render_pingall_matrix(["h1", "h2"], {("h1", "h2"): True})
    # Reverse direction is unknown → '?' cell, and not counted.
    assert "?" in out
    assert "1/1 succeeded" in out


@pytest.mark.parametrize("color", [False, True])
def test_render_pingall_color_param_does_not_crash(color: bool) -> None:
    out = render_pingall_matrix(["h1"], {}, color=color)
    assert "h1" in out
