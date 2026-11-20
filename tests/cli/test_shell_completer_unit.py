"""Unit tests for `p4net.cli.completers` and the `P4NetShell` constructor."""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.plain_text import PlainTextOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from p4net.cli import CommandDispatcher, P4NetShell
from p4net.cli.completers import build_network_completer
from p4net.cli.formatting import bold, red, render_pingall_matrix
from p4net.control import P4RuntimeError
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


def test_completer_suggests_help_topics() -> None:
    network = _network_with(["h1", "topology"], ["s1"])
    completer = build_network_completer(CommandDispatcher(network))
    event = CompleteEvent(completion_requested=True)

    assert {"h1", "s1", "<host>", "<switch>", "pingall"} <= {
        item.text for item in completer.get_completions(Document("help "), event)
    }
    for query, expected in (
        ("help h1 pi", {"ping", "ping6"}),
        ("help s1 table a", {"add"}),
        ("help <switch> counter ", {"reset"}),
        ("help topology ", {"graph"}),
        ("help unknown ", set()),
    ):
        assert {item.text for item in completer.get_completions(Document(query), event)} == expected


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


def test_shell_separates_command_output_from_the_next_prompt(tmp_path: Path) -> None:
    network = _network_with([], [])
    network.topology.to_graphviz.return_value = "digraph p4net {\n\n}\n"
    network.pingall.side_effect = [{}, KeyboardInterrupt]
    session = MagicMock()
    session.prompt.side_effect = [
        "help switches",
        "",
        "# comment",
        "pingall",
        "topology graph",
        "help unknown",
        "pingall",
        "exit",
    ]
    stream = StringIO()
    shell = P4NetShell(network, history_file=tmp_path / "history")
    with (
        patch("p4net.cli.shell.PromptSession", return_value=session),
        create_app_session(output=PlainTextOutput(stream)),
    ):
        shell.run()

    assert stream.getvalue().splitlines() == [
        "switches",
        "  List switches and their gRPC addresses / pids.",
        "",
        "(no hosts to ping)",
        "",
        "digraph p4net {",
        "",
        "}",
        "",
        "Error: no help for 'unknown'",
        "",
        "^C cancelled",
        "",
    ]


def test_shell_prints_packet_listen_progress_before_waiting(tmp_path: Path) -> None:
    network = _network_with([], ["s1"])
    client = network.switches["s1"].client
    deregister = client.on_packet_in.return_value
    session = MagicMock()
    session.prompt.side_effect = ["s1 packet listen count=2 timeout=3", "exit"]
    stream = StringIO()
    message = "Waiting for 2 packets on s1 (up to 3s)..."

    def wait(*, timeout: float) -> bool:
        assert timeout == 3.0
        client.on_packet_in.assert_called_once()
        assert stream.getvalue().splitlines() == [message]
        return False

    shell = P4NetShell(network, history_file=tmp_path / "history")
    with (
        patch("p4net.cli.shell.PromptSession", return_value=session),
        patch("threading.Event.wait", side_effect=wait),
        create_app_session(output=PlainTextOutput(stream)),
    ):
        shell.run()

    assert stream.getvalue().splitlines() == [message, "(no packets within 3.0s)", ""]
    deregister.assert_called_once()


@pytest.mark.parametrize("color", [False, True])
def test_shell_renders_errors_consistently(tmp_path: Path, color: bool) -> None:
    network = _network_with(["h1"], ["s1"])
    network.switches["s1"].client.read_counter.side_effect = P4RuntimeError(
        "no counter named 'missing'"
    )
    host = network.hosts["h1"]
    host.ping.side_effect = RuntimeError("ping unavailable\n")

    def host_output(*args: object) -> int:
        write = args[-1]
        assert callable(write)
        write("error: from host\n\n")
        return 0

    session = MagicMock()
    session.prompt.side_effect = [
        "unknown",
        "h1 ping",
        "s1 counter missing",
        "h1 ping 127.0.0.1",
        "h1 cmd printf 'error: from host\\n'",
        "exit",
    ]
    stream = StringIO()
    output = (
        Vt100_Output(
            stream, lambda: Size(rows=24, columns=80), default_color_depth=ColorDepth.DEPTH_4_BIT
        )
        if color
        else PlainTextOutput(stream)
    )
    shell = P4NetShell(network, history_file=tmp_path / "history")
    with (
        patch("p4net.cli.shell.PromptSession", return_value=session),
        patch("p4net.cli.dispatcher.stream_command_output", side_effect=host_output),
        create_app_session(output=output),
    ):
        shell.run()

    rendered = stream.getvalue()
    errors = [
        "Error: unknown command: 'unknown'",
        "Error: h1 ping: missing target",
        "Error: P4RuntimeError: no counter named 'missing'",
        "Error: RuntimeError: ping unavailable",
    ]
    if color:
        assert re.findall(r"\x1b\[[0-9;]*\b31m([^\x1b\r\n]+)", rendered) == errors
    else:
        assert "\x1b" not in rendered
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", rendered)
    expected = "\n\n".join([*errors, "error: from host", ""])
    assert plain.splitlines() == expected.splitlines()


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
def test_render_pingall_aligns_short_host_names(color: bool) -> None:
    result = {("h1", "h2"): True, ("h2", "h1"): False}
    out = render_pingall_matrix(["h1", "h2"], result, color=color)
    label = "\x1b[1mH \\ H\x1b[0m" if color else "H \\ H"
    assert [line.rstrip() for line in out.splitlines()] == [
        f"{label}  h1   h2",
        "h1     -    1",
        "h2     X    -",
        "1/2 succeeded",
    ]


def test_render_pingall_aligns_mixed_host_name_lengths() -> None:
    out = render_pingall_matrix(["h1", "hostlong"], {("h1", "hostlong"): True})
    assert [line.rstrip() for line in out.splitlines()] == [
        "H \\ H     h1        hostlong",
        "h1        -         1",
        "hostlong  ?         -",
        "1/1 succeeded",
    ]
