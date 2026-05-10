"""Unit tests for `p4net.cli.dispatcher.CommandDispatcher`. Network mocked."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from p4net.cli import CommandDispatcher
from p4net.cli.exceptions import CLIExit, CLIUsageError
from p4net.network import Network


def _make_host(
    name: str,
    primary_ip: str | None,
    ifaces: dict[str, str | None],
    *,
    primary_ip6: str | None = None,
    ifaces6: dict[str, str | None] | None = None,
) -> MagicMock:
    h = MagicMock(name=f"RunningHost-{name}")
    h.primary_ip = primary_ip
    h.primary_ip6 = primary_ip6
    h.interfaces = ifaces
    h.interfaces6 = ifaces6 or {}
    return h


def _make_switch(name: str, *, grpc: str = "127.0.0.1:50051", pid: int = 12345) -> MagicMock:
    s = MagicMock(name=f"RunningSwitch-{name}")
    bmv2 = MagicMock()
    bmv2.grpc_address = grpc
    bmv2.pid = pid
    bmv2.log_file = Path(f"/tmp/{name}.log")
    s.bmv2 = bmv2
    return s


def _make_network(
    *,
    hosts: dict[str, MagicMock] | None = None,
    switches: dict[str, MagicMock] | None = None,
    log_dir: Path | None = Path("/tmp/p4net-logs"),
    is_running: bool = True,
) -> MagicMock:
    net = MagicMock(spec=Network)
    net.hosts = hosts or {}
    net.switches = switches or {}
    net.is_running = is_running
    net.log_dir = log_dir if log_dir is not None else MagicMock(side_effect=RuntimeError)
    net.host = lambda name: net.hosts[name]
    net.switch = lambda name: net.switches[name]
    net.pingall = MagicMock(return_value={})
    return net


@pytest.fixture
def two_host_network() -> MagicMock:
    h1 = _make_host("h1", "10.0.0.1", {"h1-eth0": "10.0.0.1/24"})
    h2 = _make_host("h2", "10.0.0.2", {"h2-eth0": "10.0.0.2/24"})
    s1 = _make_switch("s1")
    return _make_network(
        hosts={"h1": h1, "h2": h2},
        switches={"s1": s1},
    )


# ---------------------------------------------------------------------------
# Empty / comment / unknown
# ---------------------------------------------------------------------------


def test_empty_line_returns_empty(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    assert d.dispatch("") == ""
    assert d.dispatch("   ") == ""


def test_comment_line_returns_empty(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    assert d.dispatch("# this is a comment") == ""


def test_unknown_command_raises(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="unknown command"):
        d.dispatch("nope")


def test_invalid_quotes_raise(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="could not parse"):
        d.dispatch('h1 cmd "unterminated')


# ---------------------------------------------------------------------------
# help / exit
# ---------------------------------------------------------------------------


def test_help_lists_every_command(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("help")
    for cmd in ("help", "exit", "quit", "status", "hosts", "switches", "pingall"):
        assert cmd in out
    assert "<host> ping" in out
    assert "<host> cmd" in out
    assert "<host> ifconfig" in out


def test_help_for_specific_topic(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("help status")
    assert "status" in out
    assert "running" in out.lower()


def test_help_for_compound_topic(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("help <host> ping")
    assert "<host> ping" in out


def test_help_for_unknown_topic_raises(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="no help"):
        d.dispatch("help nope")


def test_exit_raises_cliexit(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIExit):
        d.dispatch("exit")


def test_quit_raises_cliexit(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIExit):
        d.dispatch("quit")


# ---------------------------------------------------------------------------
# status / hosts / switches
# ---------------------------------------------------------------------------


def test_status_shows_running_state_and_counts(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("status")
    assert "running" in out.lower()
    assert "True" in out
    assert "hosts:" in out
    assert "2" in out  # 2 hosts
    assert "switches:" in out
    assert "1" in out  # 1 switch
    assert "/tmp/p4net-logs" in out


def test_hosts_lists_each_host(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("hosts")
    assert "h1" in out
    assert "h2" in out
    assert "10.0.0.1" in out
    assert "10.0.0.2" in out
    assert "h1-eth0" in out
    assert "h2-eth0" in out
    # IPv6 column rendered with '-' for v4-only hosts.
    assert "primary_ip6" in out


def test_hosts_handles_no_hosts() -> None:
    d = CommandDispatcher(_make_network(hosts={}, switches={}))
    out = d.dispatch("hosts")
    assert "no hosts" in out


def test_hosts_renders_dual_stack_and_v6_only() -> None:
    h1 = _make_host(
        "h1",
        "10.0.0.1",
        {"h1-eth0": "10.0.0.1/24"},
        primary_ip6="fd00::1",
        ifaces6={"h1-eth0": "fd00::1/64"},
    )
    h2 = _make_host(
        "h2",
        None,
        {"h2-eth0": None},
        primary_ip6="fd00::2",
        ifaces6={"h2-eth0": "fd00::2/64"},
    )
    h3 = _make_host("h3", "10.0.0.3", {"h3-eth0": "10.0.0.3/24"})
    d = CommandDispatcher(_make_network(hosts={"h1": h1, "h2": h2, "h3": h3}))
    out = d.dispatch("hosts")
    assert "fd00::1/64" in out
    assert "fd00::2/64" in out
    # h2 is v6-only; its v4 column shows '-'.
    h2_line = next(line for line in out.splitlines() if line.startswith("h2 "))
    assert " - " in h2_line
    # h3 has no v6; its v6 column shows '-'.
    h3_line = next(line for line in out.splitlines() if line.startswith("h3 "))
    assert " - " in h3_line


def test_switches_lists_each_switch(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("switches")
    assert "s1" in out
    assert "127.0.0.1:50051" in out
    assert "12345" in out
    assert "/tmp/s1.log" in out


def test_switches_handles_no_switches() -> None:
    d = CommandDispatcher(_make_network(hosts={"h1": _make_host("h1", "10.0.0.1", {})}))
    out = d.dispatch("switches")
    assert "no switches" in out


# ---------------------------------------------------------------------------
# pingall
# ---------------------------------------------------------------------------


def test_pingall_calls_network_with_default_args(two_host_network: MagicMock) -> None:
    two_host_network.pingall.return_value = {("h1", "h2"): True, ("h2", "h1"): False}
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("pingall")
    two_host_network.pingall.assert_called_once_with(count=1, timeout=2.0)
    assert "h1" in out
    assert "h2" in out
    # Render: success cell '1' for h1->h2, failure cell 'X' for h2->h1.
    assert "1" in out
    assert "X" in out
    assert "1/2 succeeded" in out


def test_pingall_parses_count_and_timeout(two_host_network: MagicMock) -> None:
    two_host_network.pingall.return_value = {("h1", "h2"): True, ("h2", "h1"): True}
    d = CommandDispatcher(two_host_network)
    d.dispatch("pingall 3 2.5")
    two_host_network.pingall.assert_called_once_with(count=3, timeout=2.5)


def test_pingall_rejects_too_many_args(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="too many"):
        d.dispatch("pingall 1 2 3")


def test_pingall_rejects_non_numeric_count(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="count must be"):
        d.dispatch("pingall abc")


# ---------------------------------------------------------------------------
# <host> commands
# ---------------------------------------------------------------------------


def test_host_ping_with_host_name_resolves_primary_ip(
    two_host_network: MagicMock,
) -> None:
    two_host_network.hosts["h1"].ping.return_value = True
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("h1 ping h2")
    assert out == "OK"
    args, kwargs = two_host_network.hosts["h1"].ping.call_args
    # First positional must be the RunningHost instance for h2 (not the string).
    assert args[0] is two_host_network.hosts["h2"]
    assert kwargs == {"count": 1, "timeout": 2.0}


def test_host_ping_with_ip_literal(two_host_network: MagicMock) -> None:
    two_host_network.hosts["h1"].ping.return_value = False
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("h1 ping 8.8.8.8 2 1.5")
    assert out == "FAIL"
    args, kwargs = two_host_network.hosts["h1"].ping.call_args
    assert args[0] == "8.8.8.8"
    assert kwargs == {"count": 2, "timeout": 1.5}


def test_host_ping_missing_target(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="missing target"):
        d.dispatch("h1 ping")


def test_host_ping_target_without_primary_ip() -> None:
    h1 = _make_host("h1", "10.0.0.1", {"h1-eth0": "10.0.0.1/24"})
    h2 = _make_host("h2", None, {"h2-eth0": None})
    d = CommandDispatcher(_make_network(hosts={"h1": h1, "h2": h2}))
    with pytest.raises(CLIUsageError, match="no primary IP"):
        d.dispatch("h1 ping h2")


def test_host_ping6_with_literal_target() -> None:
    h1 = _make_host(
        "h1",
        "10.0.0.1",
        {"h1-eth0": "10.0.0.1/24"},
        primary_ip6="fd00::1",
        ifaces6={"h1-eth0": "fd00::1/64"},
    )
    h1.ping.return_value = True
    d = CommandDispatcher(_make_network(hosts={"h1": h1}))
    out = d.dispatch("h1 ping6 fd00::ff")
    assert out == "OK"
    args, kwargs = h1.ping.call_args
    assert args[0] == "fd00::ff"
    assert kwargs == {"count": 1, "timeout": 2.0, "force_ipv6": True}


def test_host_ping6_resolves_host_to_ip6() -> None:
    h1 = _make_host(
        "h1",
        "10.0.0.1",
        {"h1-eth0": "10.0.0.1/24"},
        primary_ip6="fd00::1",
        ifaces6={"h1-eth0": "fd00::1/64"},
    )
    h2 = _make_host(
        "h2",
        "10.0.0.2",
        {"h2-eth0": "10.0.0.2/24"},
        primary_ip6="fd00::2",
        ifaces6={"h2-eth0": "fd00::2/64"},
    )
    h1.ping.return_value = True
    d = CommandDispatcher(_make_network(hosts={"h1": h1, "h2": h2}))
    out = d.dispatch("h1 ping6 h2")
    assert out == "OK"
    args, kwargs = h1.ping.call_args
    assert args[0] == "fd00::2"
    assert kwargs["force_ipv6"] is True


def test_host_ping6_missing_target() -> None:
    h1 = _make_host("h1", "10.0.0.1", {"h1-eth0": "10.0.0.1/24"})
    d = CommandDispatcher(_make_network(hosts={"h1": h1}))
    with pytest.raises(CLIUsageError, match="missing target"):
        d.dispatch("h1 ping6")


def test_host_ping6_host_without_ip6() -> None:
    h1 = _make_host("h1", "10.0.0.1", {"h1-eth0": "10.0.0.1/24"})
    h2 = _make_host("h2", "10.0.0.2", {"h2-eth0": "10.0.0.2/24"})
    d = CommandDispatcher(_make_network(hosts={"h1": h1, "h2": h2}))
    with pytest.raises(CLIUsageError, match="no primary IPv6"):
        d.dispatch("h1 ping6 h2")


def test_host_cmd_renders_stdout(two_host_network: MagicMock) -> None:
    two_host_network.hosts["h1"].exec.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b"hello\n", stderr=b""
    )
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("h1 cmd echo hi")
    assert out == "hello"
    argv, _ = two_host_network.hosts["h1"].exec.call_args
    assert argv[0] == ["echo", "hi"]


def test_host_cmd_quoted_argv_preserved(two_host_network: MagicMock) -> None:
    two_host_network.hosts["h1"].exec.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b"a b\n", stderr=b""
    )
    d = CommandDispatcher(two_host_network)
    d.dispatch('h1 cmd echo "a b"')
    argv, _ = two_host_network.hosts["h1"].exec.call_args
    assert argv[0] == ["echo", "a b"]


def test_host_cmd_renders_stderr_with_prefix(two_host_network: MagicMock) -> None:
    two_host_network.hosts["h1"].exec.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b"", stderr=b"warn line\n"
    )
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("h1 cmd noisy")
    assert out == "[stderr] warn line"


def test_host_cmd_appends_exit_code_when_nonzero(two_host_network: MagicMock) -> None:
    two_host_network.hosts["h1"].exec.return_value = subprocess.CompletedProcess(
        args=[], returncode=2, stdout=b"out\n", stderr=b"err\n"
    )
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("h1 cmd false")
    assert "out" in out
    assert "[stderr] err" in out
    assert "[exit 2]" in out


def test_host_cmd_missing_argv(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="missing argv"):
        d.dispatch("h1 cmd")


def test_host_ifconfig_runs_ip_br_addr(two_host_network: MagicMock) -> None:
    two_host_network.hosts["h1"].exec.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b"lo UNKNOWN ...\n", stderr=b""
    )
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("h1 ifconfig")
    assert "lo" in out
    argv, _ = two_host_network.hosts["h1"].exec.call_args
    assert argv[0] == ["ip", "-br", "addr"]


def test_host_ifconfig_rejects_extra_args(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="takes no arguments"):
        d.dispatch("h1 ifconfig extra")


def test_host_unknown_verb(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="unknown verb"):
        d.dispatch("h1 fly")


def test_host_missing_verb(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="missing verb"):
        d.dispatch("h1")


# ---------------------------------------------------------------------------
# Color OFF / ON
# ---------------------------------------------------------------------------


def test_color_off_yields_no_ansi(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network, color=False)
    out = d.dispatch("status")
    assert "\x1b[" not in out


def test_color_on_uses_ansi(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network, color=True)
    out = d.dispatch("status")
    assert "\x1b[" in out


# ---------------------------------------------------------------------------
# Properties for the completer
# ---------------------------------------------------------------------------


def test_command_names_lists_top_level(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    cmds = d.command_names
    assert "help" in cmds
    assert "pingall" in cmds


def test_host_and_switch_names(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    assert d.host_names == ["h1", "h2"]
    assert d.switch_names == ["s1"]


def test_status_handles_log_dir_unavailable() -> None:
    """When `network.log_dir` raises (pre-start), status renders gracefully."""

    class _FailingLogDir(MagicMock):
        @property
        def log_dir(self) -> Any:  # type: ignore[override]
            raise RuntimeError("not yet allocated")

    n = _FailingLogDir(spec=Network)
    n.hosts = {}
    n.switches = {}
    n.is_running = False
    d = CommandDispatcher(n)
    out = d.dispatch("status")
    assert "not allocated" in out


# ---------------------------------------------------------------------------
# pingall6 + <host> xterm (phase 13)
# ---------------------------------------------------------------------------


def test_pingall6_renders_matrix() -> None:
    h1 = _make_host(
        "h1",
        "10.0.0.1",
        {"h1-eth0": "10.0.0.1/24"},
        primary_ip6="fd00::1",
        ifaces6={"h1-eth0": "fd00::1/64"},
    )
    h2 = _make_host(
        "h2",
        "10.0.0.2",
        {"h2-eth0": "10.0.0.2/24"},
        primary_ip6="fd00::2",
        ifaces6={"h2-eth0": "fd00::2/64"},
    )
    net = _make_network(hosts={"h1": h1, "h2": h2})
    net.pingall6 = MagicMock(return_value={("h1", "h2"): True, ("h2", "h1"): True})
    d = CommandDispatcher(net)
    out = d.dispatch("pingall6 2 1.0")
    net.pingall6.assert_called_once_with(count=2, timeout=1.0)
    assert "h1" in out
    assert "h2" in out
    assert "2/2 succeeded" in out


def test_pingall6_empty_returns_placeholder() -> None:
    h1 = _make_host("h1", "10.0.0.1", {"h1-eth0": "10.0.0.1/24"})
    net = _make_network(hosts={"h1": h1})
    d = CommandDispatcher(net)
    out = d.dispatch("pingall6")
    assert out == "(no IPv6-equipped hosts in topology)"


def test_host_xterm_renders_pid(two_host_network: MagicMock) -> None:
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    two_host_network.xterm = MagicMock(return_value=fake_proc)
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("h1 xterm")
    two_host_network.xterm.assert_called_once_with("h1")
    assert out == "xterm spawned (pid=4242)"


def test_host_xterm_renders_error_on_exception(two_host_network: MagicMock) -> None:
    two_host_network.xterm = MagicMock(side_effect=RuntimeError("DISPLAY missing"))
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("h1 xterm")
    assert out.startswith("error: RuntimeError:")


def test_host_xterm_rejects_arguments(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="takes no arguments"):
        d.dispatch("h1 xterm extra")


# ---------------------------------------------------------------------------
# topology graph (phase 13)
# ---------------------------------------------------------------------------


def test_topology_graph_no_path_prints_dot(two_host_network: MagicMock) -> None:
    fake_topo = MagicMock()
    fake_topo.to_graphviz = MagicMock(return_value="digraph p4net {\n}\n")
    two_host_network.topology = fake_topo
    d = CommandDispatcher(two_host_network)
    out = d.dispatch("topology graph")
    fake_topo.to_graphviz.assert_called_once_with(layout="LR")
    assert out.startswith("digraph p4net")


def test_topology_graph_format_dot_writes_file(two_host_network: MagicMock, tmp_path: Path) -> None:
    fake_topo = MagicMock()
    two_host_network.topology = fake_topo
    out_path = tmp_path / "g.dot"
    d = CommandDispatcher(two_host_network)
    out = d.dispatch(f"topology graph {out_path} format=dot")
    fake_topo.render_graphviz.assert_called_once_with(out_path, layout="LR", format="dot")
    assert out == str(out_path.resolve())


def test_topology_graph_default_format_is_png(two_host_network: MagicMock, tmp_path: Path) -> None:
    fake_topo = MagicMock()
    two_host_network.topology = fake_topo
    out_path = tmp_path / "g.png"
    d = CommandDispatcher(two_host_network)
    d.dispatch(f"topology graph {out_path}")
    fake_topo.render_graphviz.assert_called_once_with(out_path, layout="LR", format="png")


def test_topology_graph_layout_kwarg(two_host_network: MagicMock) -> None:
    fake_topo = MagicMock()
    fake_topo.to_graphviz = MagicMock(return_value="digraph p4net {\n}\n")
    two_host_network.topology = fake_topo
    d = CommandDispatcher(two_host_network)
    d.dispatch("topology graph layout=TB")
    fake_topo.to_graphviz.assert_called_once_with(layout="TB")


def test_topology_graph_unknown_option(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="unknown option"):
        d.dispatch("topology graph nope=42")


def test_topology_graph_render_failure_renders_error(
    two_host_network: MagicMock, tmp_path: Path
) -> None:
    fake_topo = MagicMock()
    fake_topo.render_graphviz = MagicMock(side_effect=RuntimeError("dot died"))
    two_host_network.topology = fake_topo
    d = CommandDispatcher(two_host_network)
    out = d.dispatch(f"topology graph {tmp_path / 'g.png'}")
    assert out.startswith("error: RuntimeError:")


def test_topology_graph_validates_before_rendering() -> None:
    """A malformed topology surfaces validate()'s error instead of dumping DOT."""
    from p4net.topo import Link, LinkEndpoint, Topology
    from p4net.topo.exceptions import TopologyError

    real_topo = Topology()
    real_topo.add_host("h1")
    real_topo.add_switch("s1", Path("p.p4"))
    # Bypass add_link's validation: append a Link that references a node
    # which does not exist. validate() should catch the dangling endpoint.
    real_topo._links.append(  # type: ignore[attr-defined]
        Link(a=LinkEndpoint(node="h1"), b=LinkEndpoint(node="ghost"))
    )
    with pytest.raises(TopologyError):
        real_topo.validate()
    # Confirm via the dispatcher that the error is rendered, not the DOT.
    net = _make_network(hosts={"h1": _make_host("h1", "10.0.0.1", {})})
    net.topology = real_topo
    d = CommandDispatcher(net)
    out = d.dispatch("topology graph")
    assert out.startswith("error: TopologyError:")
    assert "ghost" in out
    assert "digraph" not in out


def test_topology_unknown_subverb(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="unknown sub-verb"):
        d.dispatch("topology bogus")


def test_topology_missing_subverb(two_host_network: MagicMock) -> None:
    d = CommandDispatcher(two_host_network)
    with pytest.raises(CLIUsageError, match="missing sub-verb"):
        d.dispatch("topology")
