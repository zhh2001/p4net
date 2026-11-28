"""Command parser/executor for the p4net interactive shell.

The dispatcher takes a `Network`, accepts a single input line, and returns
formatted output. An optional callback receives progress messages before a
command returns. The shell handles terminal rendering.
"""

from __future__ import annotations

import logging
import math
import re
import shlex
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from textwrap import fill
from time import monotonic
from typing import TYPE_CHECKING

from p4net.cli._command_output import stream_command_output
from p4net.cli.exceptions import CLIExit, CLIUsageError, _CommandErrorOutput
from p4net.cli.formatting import bold, render_pingall_matrix
from p4net.network._cleanup import defer_cleanup_signals
from p4net.network.orchestrator import _hosts_with_ip, _ping_host_pairs

if TYPE_CHECKING:
    from p4net.control import P4InfoIndex
    from p4net.network import Network, RunningHost
    from p4net.network.nodes import RunningSwitch

_log = logging.getLogger(__name__)

# poll(2) accepts signed 32-bit millisecond timeouts.
_MAX_PING_WAIT = ((1 << 31) - 1) // 1000
_HOST_COMMAND_TIMEOUT = 30
_PING_REPLY = re.compile(r"^\d+ bytes from .*?\bicmp_seq=(\d+)\b.*? (time[=<][\d.]+ ms)")
_PING_STATS = re.compile(
    r"^(\d+) packets transmitted, (\d+) (?:packets )?received.*?, (\S+)% packet loss\b"
)

# A switch verb handler takes (switch_name, remaining tokens) and returns
# the formatted output string.
SwitchHandler = Callable[[str, list[str]], str]


# Keep help layout consistent across terminal widths, with room for 60-column terminals.
_HELP_WIDTH = 60
_HELP_TOPIC_WIDTH = 22

# Help topics map to a short description and a usage synopsis.
_TOPIC_HELP: dict[str, tuple[str, str]] = {
    "help": ("List commands or show details for one.", "help [command]"),
    "exit": ("Exit the shell.", "exit"),
    "quit": ("Exit the shell.", "quit"),
    "status": ("Print network status (running, counts, log dir).", "status"),
    "hosts": ("List hosts and their primary IPs / interfaces.", "hosts"),
    "switches": ("List switches and their gRPC addresses / pids.", "switches"),
    "pingall": (
        "Check IPv4 host pairs and show a result matrix with progress.",
        "pingall [count] [timeout]",
    ),
    "pingall6": (
        "Check IPv6-equipped host pairs and show a result matrix with progress.",
        "pingall6 [count] [timeout]",
    ),
    "topology graph": (
        "Render the topology as a Graphviz DOT graph (or to PNG/SVG/PDF).",
        "topology graph [path] [layout=LR|TB|BT|RL]\n    [format=png|svg|pdf|dot]",
    ),
    "<host> ping": (
        "Ping a target from a host. Multiple probes show live replies and totals.",
        "<host> ping <target> [count] [timeout]",
    ),
    "<host> ping6": (
        "Ping a target using IPv6. Multiple probes show live replies and totals.",
        "<host> ping6 <target> [count] [timeout]",
    ),
    "<host> cmd": (
        "Run a command inside a host's namespace.",
        "<host> cmd <argv ...>",
    ),
    "<host> ifconfig": (
        "Show host interfaces (ip -br addr).",
        "<host> ifconfig",
    ),
    "<host> xterm": (
        "Spawn an interactive xterm in a host's namespace (requires $DISPLAY).",
        "<host> xterm",
    ),
    "<switch> log": (
        "Print the absolute path of the switch's BMv2 log file.",
        "<switch> log",
    ),
    "<switch> table list": (
        "List the switch's tables and their match-key types.",
        "<switch> table list",
    ),
    "<switch> table dump": (
        "Print every entry in the named table.",
        "<switch> table dump <table>",
    ),
    "<switch> table add": (
        "Insert a table entry (ternary value&&&mask, range low..high).",
        "<switch> table add <table>\n"
        "    match: <k>=<v>[,<k>=<v>...] action: <name>\n"
        "    [params: <k>=<v>[,<k>=<v>...]] [priority: <int>]",
    ),
    "<switch> table del": (
        "Delete a table entry (ternary value&&&mask, range low..high).",
        "<switch> table del <table>\n    match: <k>=<v>[,<k>=<v>...] [priority: <int>]",
    ),
    "<switch> table clear": (
        "Delete every entry from a table.",
        "<switch> table clear <table>",
    ),
    "<switch> counter": (
        "Read a counter cell or list returned cells.",
        "<switch> counter <name> [<index> | --all | --nonzero]",
    ),
    "<switch> counter reset": (
        "Zero a counter cell or every cell.",
        "<switch> counter reset <name> [<index>]",
    ),
    "<switch> mcast list": (
        "List multicast groups.",
        "<switch> mcast list",
    ),
    "<switch> mcast add": (
        "Create a multicast group.",
        "<switch> mcast add <id> <port>[,<port>...]",
    ),
    "<switch> mcast del": (
        "Delete a multicast group.",
        "<switch> mcast del <id>",
    ),
    "<switch> packet send": (
        "Inject a PacketOut into the switch.",
        "<switch> packet send <hex_payload>\n    [metadata: <k>=<v>[,<k>=<v>...]]",
    ),
    "<switch> packet listen": (
        "Show PacketIns as they arrive until the count or timeout.",
        "<switch> packet listen [count=<int>] [timeout=<float>]",
    ),
}


class CommandDispatcher:
    """Parse and execute p4net CLI commands against a running Network."""

    def __init__(
        self,
        network: Network,
        *,
        color: bool = False,
        on_progress: Callable[[str], None] | None = None,
        on_command_output: Callable[[str], None] | None = None,
    ) -> None:
        self._network = network
        self._color = color
        self._on_progress = on_progress
        self._on_command_output = on_command_output
        self._top_level_handlers = {
            "help": self._cmd_help,
            "exit": self._cmd_exit,
            "quit": self._cmd_exit,
            "status": self._cmd_status,
            "hosts": self._cmd_hosts,
            "switches": self._cmd_switches,
            "pingall": self._cmd_pingall,
            "pingall6": self._cmd_pingall6,
            "topology": self._cmd_topology,
        }
        self._host_handlers = {
            "ping": self._cmd_host_ping,
            "ping6": self._cmd_host_ping6,
            "cmd": self._cmd_host_cmd,
            "ifconfig": self._cmd_host_ifconfig,
            "xterm": self._cmd_host_xterm,
        }
        self._switch_handlers: dict[str, SwitchHandler] = {
            "log": self._cmd_switch_log,
            "table": self._cmd_switch_table,
            "counter": self._cmd_switch_counter,
            "mcast": self._cmd_switch_mcast,
            "packet": self._cmd_switch_packet,
        }
        # Help registry can be extended by subclasses or commit 3 monkey-patch.
        self._help_topics: dict[str, tuple[str, str]] = dict(_TOPIC_HELP)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dispatch(self, line: str) -> str:
        """Parse one input line and return the output string.

        Empty lines and `#`-prefixed comment lines return an empty string.
        Unknown commands raise `CLIUsageError`. The `exit`/`quit` commands
        raise `CLIExit`.
        """
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return ""
        try:
            tokens = shlex.split(stripped)
        except ValueError as exc:
            raise CLIUsageError(f"could not parse command: {exc}") from exc
        if not tokens:
            return ""
        head = tokens[0]
        rest = tokens[1:]
        if head in self._top_level_handlers:
            return self._top_level_handlers[head](rest)
        if head in self._network.hosts:
            return self._dispatch_host(head, rest)
        if head in self._network.switches:
            return self._dispatch_switch(head, rest)
        raise CLIUsageError(f"unknown command: {head!r}")

    @property
    def command_names(self) -> list[str]:
        """Names of every top-level command this dispatcher knows about."""
        return list(self._top_level_handlers.keys())

    @property
    def host_names(self) -> list[str]:
        """Names of the hosts in the underlying ``Network``."""
        return list(self._network.hosts.keys())

    @property
    def switch_names(self) -> list[str]:
        """Names of the switches in the underlying ``Network``."""
        return list(self._network.switches.keys())

    @property
    def color(self) -> bool:
        """``True`` if ANSI colour escapes should be emitted in output."""
        return self._color

    # ------------------------------------------------------------------
    # Internal: top-level commands
    # ------------------------------------------------------------------

    def _cmd_help(self, tokens: list[str]) -> str:
        if not tokens:
            return self._help_overview()
        topic = " ".join(tokens)
        info = self._help_topics.get(topic)
        node = tokens[0]
        prefix = self._node_help_prefix(node) if info is None else None
        if prefix is not None:
            info = self._help_topics.get(" ".join([prefix, *tokens[1:]]))
        if info is None:
            raise CLIUsageError(f"no help for {topic!r}")
        desc, usage = info
        if prefix is not None:
            usage = usage.replace(prefix, node, 1)
        description = fill(
            desc,
            width=_HELP_WIDTH,
            initial_indent="  ",
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
        return f"{bold(usage, color=self._color)}\n{description}"

    def _help_overview(self) -> str:
        groups: dict[str, tuple[str, list[str]]] = {
            "": ("General commands", []),
            "<host>": ("Host commands", []),
            "<switch>": ("Switch commands", []),
        }
        for topic, (desc, _) in self._help_topics.items():
            _, rows = groups.get(topic.partition(" ")[0], groups[""])
            label = f"  {topic:<{_HELP_TOPIC_WIDTH}}  "
            rows.append(
                fill(
                    desc,
                    width=_HELP_WIDTH,
                    initial_indent=label,
                    subsequent_indent=" " * len(label),
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
        sections = [
            "\n".join([bold(title, color=self._color), *rows])
            for title, rows in groups.values()
            if rows
        ]
        return "\n\n".join(sections)

    def _node_help_prefix(self, name: str) -> str | None:
        if name in self._top_level_handlers:
            return None
        if name in self._network.hosts:
            return "<host>"
        if name in self._network.switches:
            return "<switch>"
        return None

    def _cmd_exit(self, tokens: list[str]) -> str:
        raise CLIExit

    def _cmd_status(self, tokens: list[str]) -> str:
        lines = [bold("Network status", color=self._color)]
        lines.append(f"  running:   {self._network.is_running}")
        lines.append(f"  hosts:     {len(self._network.hosts)}")
        lines.append(f"  switches:  {len(self._network.switches)}")
        try:
            log_dir = self._network.log_dir
        except RuntimeError:
            lines.append("  log_dir:   <not allocated>")
        else:
            lines.append(f"  log_dir:   {log_dir}")
        return "\n".join(lines)

    def _cmd_hosts(self, tokens: list[str]) -> str:
        hosts = self._network.hosts
        if not hosts:
            return "(no hosts)"
        rows = [("name", "primary_ip", "primary_ip6", "interfaces")]
        for name, host in hosts.items():
            cidr4 = next((c for c in host.interfaces.values() if c), None) or "-"
            cidr6 = (
                next(
                    (c for c in getattr(host, "interfaces6", {}).values() if c),
                    None,
                )
                or "-"
            )
            ifaces = ", ".join(host.interfaces) if host.interfaces else "-"
            rows.append((name, cidr4, cidr6, ifaces))
        name_w = max(len(row[0]) for row in rows)
        ip_w = max(len(row[1]) for row in rows)
        ip6_w = max(len(row[2]) for row in rows)
        lines = [
            f"{name:<{name_w}}  {ip:<{ip_w}}  {ip6:<{ip6_w}}  {ifaces}"
            for name, ip, ip6, ifaces in rows
        ]
        lines[0] = bold(lines[0], color=self._color)
        return "\n".join(lines)

    def _cmd_switches(self, tokens: list[str]) -> str:
        switches = self._network.switches
        if not switches:
            return "(no switches)"
        rows: list[tuple[str, str, str, str]] = []
        for name, sw in switches.items():
            bmv2 = sw.bmv2
            pid = str(bmv2.pid) if bmv2.pid is not None else "-"
            rows.append((name, bmv2.grpc_address, pid, str(bmv2.log_file)))
        name_w = max(4, max(len(r[0]) for r in rows))
        addr_w = max(10, max(len(r[1]) for r in rows))
        pid_w = max(3, max(len(r[2]) for r in rows))
        header = bold(
            f"{'name'.ljust(name_w)}  {'grpc_addr'.ljust(addr_w)}  {'pid'.ljust(pid_w)}  log_file",
            color=self._color,
        )
        lines = [header]
        for name, addr, pid, log in rows:
            lines.append(f"{name.ljust(name_w)}  {addr.ljust(addr_w)}  {pid.ljust(pid_w)}  {log}")
        return "\n".join(lines)

    def _cmd_pingall(self, tokens: list[str], *, ipv6: bool = False) -> str:
        count, timeout = self._parse_count_timeout(tokens, label="pingall6" if ipv6 else "pingall")
        hosts = self._network.hosts
        eligible = _hosts_with_ip(hosts, ipv6=ipv6)
        if ipv6 and not eligible:
            return "(no IPv6-equipped hosts in topology)"
        matrix_hosts = list(eligible if ipv6 else hosts)
        if self._on_progress is not None and len(eligible) > 1:
            return self._interactive_pingall(matrix_hosts, eligible, count, timeout, ipv6)
        pingall = self._network.pingall6 if ipv6 else self._network.pingall
        result = pingall(count=count, timeout=timeout)
        return render_pingall_matrix(matrix_hosts, result, color=self._color)

    def _cmd_pingall6(self, tokens: list[str]) -> str:
        return self._cmd_pingall(tokens, ipv6=True)

    def _interactive_pingall(
        self,
        matrix_hosts: list[str],
        eligible: dict[str, RunningHost],
        count: int,
        timeout: float,
        ipv6: bool,
    ) -> str:
        progress = self._on_progress
        assert progress is not None
        total = len(eligible) * (len(eligible) - 1)
        show_progress = count > 1 or len(eligible) > 2
        batch = (total + 3) // 4
        last_reported = -1
        results: dict[tuple[str, str], bool] = {}

        if show_progress:
            progress(f"Pinging {total} host pairs...")
        try:
            for pair, ok in _ping_host_pairs(eligible, count=count, timeout=timeout, ipv6=ipv6):
                results[pair] = ok
                checked = len(results)
                if show_progress and checked < total and checked % batch == 0:
                    progress(f"{checked}/{total} host pairs checked")
                    last_reported = checked
        except KeyboardInterrupt:
            if len(results) != last_reported:
                progress(f"{len(results)}/{total} host pairs checked")
            progress(render_pingall_matrix(matrix_hosts, results, color=False))
            raise
        return render_pingall_matrix(matrix_hosts, results, color=self._color)

    def _cmd_topology(self, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError("topology: missing sub-verb (try 'topology graph')")
        sub, rest = tokens[0], tokens[1:]
        if sub == "graph":
            return self._cmd_topology_graph(rest)
        raise CLIUsageError(f"topology: unknown sub-verb {sub!r} (expected 'graph')")

    def _cmd_topology_graph(self, tokens: list[str]) -> str:
        from pathlib import Path as _Path

        # Parse: optional positional path + k=v options.
        path: _Path | None = None
        kvs: dict[str, str] = {}
        for tok in tokens:
            if "=" in tok:
                k, _, v = tok.partition("=")
                kvs[k] = v
            elif path is None:
                path = _Path(tok)
            else:
                raise CLIUsageError(f"topology graph: unexpected token {tok!r} after path {path!s}")
        for k in kvs:
            if k not in ("layout", "format"):
                raise CLIUsageError(
                    f"topology graph: unknown option {k!r} (expected layout=, format=)"
                )
        layout = kvs.get("layout", "LR")
        fmt = kvs.get("format", "png")
        topo = self._network.topology
        # Validate before rendering so a malformed topology surfaces a clear
        # error instead of a silently-misleading DOT graph.
        try:
            topo.validate()
        except Exception as exc:
            return _CommandErrorOutput.from_exception(exc)
        if path is None:
            return topo.to_graphviz(layout=layout)
        try:
            topo.render_graphviz(path, layout=layout, format=fmt)
        except Exception as exc:
            return _CommandErrorOutput.from_exception(exc)
        return str(path.resolve())

    # ------------------------------------------------------------------
    # Internal: host commands
    # ------------------------------------------------------------------

    def _dispatch_host(self, host_name: str, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError(
                f"host {host_name!r}: missing verb "
                f"(try '{host_name} ifconfig' or '{host_name} ping <target>')"
            )
        verb, rest = tokens[0], tokens[1:]
        handler = self._host_handlers.get(verb)
        if handler is None:
            raise CLIUsageError(
                f"host {host_name!r}: unknown verb {verb!r} (known: {sorted(self._host_handlers)})"
            )
        return handler(host_name, rest)

    def _cmd_host_ping(self, host_name: str, tokens: list[str], *, force_ipv6: bool = False) -> str:
        verb = "ping6" if force_ipv6 else "ping"
        if not tokens:
            raise CLIUsageError(f"{host_name} {verb}: missing target")
        target = tokens[0]
        count, timeout = self._parse_count_timeout(tokens[1:], label=f"{host_name} {verb}")
        host = self._network.host(host_name)
        address = target
        target_host = self._network.hosts.get(target)
        if target_host is not None:
            primary = target_host.primary_ip6 if force_ipv6 else target_host.primary_ip
            if primary is None:
                family = "IPv6" if force_ipv6 else "IP"
                raise CLIUsageError(f"target {target!r}: no primary {family} configured")
            address = primary
        if count > 1 and self._on_progress is not None:
            heading = f"{target} ({address})" if target_host is not None else target
            return self._interactive_ping(host, heading, address, count, timeout, force_ipv6)
        if force_ipv6:
            ok = host.ping(address, count=count, timeout=timeout, force_ipv6=True)
        else:
            ok = host.ping(target_host or address, count=count, timeout=timeout)
        return "OK" if ok else "FAIL"

    def _cmd_host_ping6(self, host_name: str, tokens: list[str]) -> str:
        return self._cmd_host_ping(host_name, tokens, force_ipv6=True)

    def _interactive_ping(
        self,
        host: RunningHost,
        heading: str,
        address: str,
        count: int,
        timeout: float,
        force_ipv6: bool,
    ) -> str:
        progress = self._on_progress
        assert progress is not None
        summary = ""
        error = ""

        def report(line: str) -> None:
            nonlocal summary, error
            if reply := _PING_REPLY.match(line):
                seq, elapsed = reply.groups()
                duplicate = " (DUP!)" if "(DUP!)" in line else ""
                progress(f"seq={seq}  {elapsed}{duplicate}")
            elif stats := _PING_STATS.match(line):
                sent, received, loss = stats.groups()
                summary = f"{sent} sent, {received} received, {loss}% loss"
            elif line.startswith(("ping:", "Cannot open network namespace", "exec of")):
                error = line

        progress(f"PING {heading}")
        try:
            result = host._ping_with_output(
                address, count=count, timeout=timeout, force_ipv6=force_ipv6, on_line=report
            )
        except KeyboardInterrupt:
            if summary:
                progress(summary)
            raise
        if summary:
            return summary
        if error:
            return _CommandErrorOutput(f"error: {error}")
        return "OK" if result == 0 else "FAIL"

    def _cmd_host_cmd(self, host_name: str, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError(f"{host_name} cmd: missing argv")
        host = self._network.host(host_name)
        output = self._on_command_output
        if output is None:
            result = host.exec(
                tokens, timeout=_HOST_COMMAND_TIMEOUT, check=False, capture_output=True
            )
            return self._format_cmd_result(result)
        try:
            with self._network._host_command(host, tokens) as process:
                rc = stream_command_output(process, tokens, _HOST_COMMAND_TIMEOUT, output)
        except subprocess.TimeoutExpired:
            return _CommandErrorOutput(
                f"error: command timed out after {_HOST_COMMAND_TIMEOUT} seconds"
            )
        return f"[exit {rc}]" if rc else ""

    def _cmd_host_ifconfig(self, host_name: str, tokens: list[str]) -> str:
        if tokens:
            raise CLIUsageError(f"{host_name} ifconfig: takes no arguments")
        host = self._network.host(host_name)
        result = host.exec(["ip", "-br", "addr"], check=False, capture_output=True)
        return result.stdout.decode("utf-8", errors="replace").rstrip("\n")

    def _cmd_host_xterm(self, host_name: str, tokens: list[str]) -> str:
        if tokens:
            raise CLIUsageError(f"{host_name} xterm: takes no arguments")
        try:
            proc = self._network.xterm(host_name)
        except Exception as exc:
            return _CommandErrorOutput.from_exception(exc)
        return f"xterm spawned (pid={proc.pid})"

    # ------------------------------------------------------------------
    # Internal: switch dispatcher (extended in commit 3)
    # ------------------------------------------------------------------

    def _dispatch_switch(self, switch_name: str, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError(
                f"switch {switch_name!r}: missing verb (try 'help' or '{switch_name} log')"
            )
        verb = tokens[0]
        handler = self._switch_handlers.get(verb)
        if handler is None:
            raise CLIUsageError(
                f"switch {switch_name!r}: unknown verb {verb!r} "
                f"(known: {sorted(self._switch_handlers)})"
            )
        return handler(switch_name, tokens[1:])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_count_timeout(tokens: list[str], *, label: str) -> tuple[int, float]:
        if len(tokens) > 2:
            raise CLIUsageError(f"{label}: too many arguments")
        count = 1
        timeout = 2.0
        if len(tokens) >= 1:
            try:
                count = int(tokens[0])
            except ValueError as exc:
                raise CLIUsageError(
                    f"{label}: count must be an integer, got {tokens[0]!r}"
                ) from exc
        if len(tokens) >= 2:
            try:
                timeout = float(tokens[1])
            except ValueError as exc:
                raise CLIUsageError(
                    f"{label}: timeout must be a number, got {tokens[1]!r}"
                ) from exc
        if count <= 0:
            raise CLIUsageError(f"{label}: count must be a positive integer, got {tokens[0]!r}")
        if not math.isfinite(timeout) or timeout < 0:
            raise CLIUsageError(
                f"{label}: timeout must be a finite non-negative number, got {tokens[1]!r}"
            )
        wait = math.inf
        if count <= _MAX_PING_WAIT:
            wait = max(count * timeout, count - 1 + timeout)
        if not math.isfinite(wait) or math.ceil(wait) + 1 > _MAX_PING_WAIT:
            raise CLIUsageError(
                f"{label}: count and timeout exceed the maximum ping wait "
                f"of {_MAX_PING_WAIT} seconds"
            )
        return count, timeout

    # ------------------------------------------------------------------
    # Internal: switch verbs
    # ------------------------------------------------------------------

    def _cmd_switch_log(self, switch_name: str, tokens: list[str]) -> str:
        if tokens:
            raise CLIUsageError(f"{switch_name} log: takes no arguments")
        sw = self._network.switch(switch_name)
        return str(sw.bmv2.log_file)

    def _cmd_switch_table(self, switch_name: str, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError(f"{switch_name} table: missing sub-verb (list/dump/add/del/clear)")
        sub, rest = tokens[0], tokens[1:]
        sw = self._network.switch(switch_name)
        if sub == "list":
            return self._table_list(sw, rest)
        if sub == "dump":
            return self._table_dump(sw, rest)
        if sub == "add":
            return self._table_add(sw, rest)
        if sub == "del":
            return self._table_del(sw, rest)
        if sub == "clear":
            return self._table_clear(sw, rest)
        raise CLIUsageError(
            f"{switch_name} table: unknown sub-verb {sub!r} "
            f"(expected one of: list, dump, add, del, clear)"
        )

    def _table_list(self, switch: object, tokens: list[str]) -> str:
        if tokens:
            raise CLIUsageError("table list: takes no arguments")
        index = switch.client.index  # type: ignore[attr-defined]
        rows: list[str] = []
        for table in index.raw.tables:
            mfs = []
            for mf in table.match_fields:
                # Map match_type enum to a friendly name.
                mt_name = _match_type_label(mf.match_type)
                mfs.append(f"{mt_name}: {mf.name}")
            joined = ", ".join(mfs) if mfs else "no match fields"
            rows.append(f"{table.preamble.name}  ({joined})")
        if not rows:
            return "(no tables)"
        return "\n".join(rows)

    def _table_dump(self, switch: object, tokens: list[str]) -> str:
        if len(tokens) != 1:
            raise CLIUsageError("table dump: requires exactly one table name")
        table = tokens[0]
        entries = switch.client.list_table_entries(table)  # type: ignore[attr-defined]
        if not entries:
            return f"(table {table!r} is empty)"
        index = switch.client.index  # type: ignore[attr-defined]
        count = len(entries)
        entry_label = "entry" if count == 1 else "entries"
        lines = [bold(f"{table} ({count} {entry_label})", color=self._color)]
        for i, entry in enumerate(entries):
            if i:
                lines.append("")
            lines.append(f"#{i}")
            raw_match = entry.get("match", {})
            try:
                rendered_match: dict[str, str] = index.decode_match(table, raw_match)
            except Exception as exc:
                _log.debug("decode_match failed for %s: %s; falling back to raw bytes", table, exc)
                rendered_match = {name: repr(value) for name, value in raw_match.items()}
            lines.extend(_format_table_fields("match", rendered_match))
            action_name = entry.get("action")
            lines.append(f"  action:   {action_name}")
            params = entry.get("params") or {}
            if params:
                rendered_params = _render_action_params(index, action_name, params)
                lines.extend(_format_table_fields("params", rendered_params))
            if entry.get("priority") is not None:
                lines.append(f"  priority: {entry['priority']}")
        return "\n".join(lines)

    def _table_add(self, switch: object, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError("table add: missing table name")
        table = tokens[0]
        sections = _parse_sections(
            tokens[1:],
            allowed={"match", "action", "params", "priority"},
        )
        if "match" not in sections:
            raise CLIUsageError("table add: 'match:' section is required")
        if "action" not in sections:
            raise CLIUsageError("table add: 'action:' section is required")
        match = _parse_kv_pairs(sections["match"])
        action_tokens = sections["action"]
        if len(action_tokens) != 1:
            raise CLIUsageError(
                f"table add: 'action:' must be a single token, got {action_tokens!r}"
            )
        action = action_tokens[0]
        params = _parse_kv_pairs(sections["params"]) if "params" in sections else None
        priority = _parse_priority(sections.get("priority"))
        kwargs: dict[str, object] = {}
        if priority is not None:
            kwargs["priority"] = priority
        try:
            typed_match = _parse_table_match(switch.client.index, table, match)  # type: ignore[attr-defined]
            switch.client.insert_table_entry(  # type: ignore[attr-defined]
                table, typed_match, action, params, **kwargs
            )
        except CLIUsageError:
            raise
        except Exception as exc:
            return _CommandErrorOutput.from_exception(exc)
        return "ok"

    def _table_del(self, switch: object, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError("table del: missing table name")
        table = tokens[0]
        sections = _parse_sections(tokens[1:], allowed={"match", "priority"})
        if "match" not in sections:
            raise CLIUsageError("table del: 'match:' section is required")
        match = _parse_kv_pairs(sections["match"])
        priority = _parse_priority(sections.get("priority"))
        kwargs: dict[str, object] = {}
        if priority is not None:
            kwargs["priority"] = priority
        try:
            typed_match = _parse_table_match(switch.client.index, table, match)  # type: ignore[attr-defined]
            switch.client.delete_table_entry(table, typed_match, **kwargs)  # type: ignore[attr-defined]
        except CLIUsageError:
            raise
        except Exception as exc:
            return _CommandErrorOutput.from_exception(exc)
        return "ok"

    def _table_clear(self, switch: object, tokens: list[str]) -> str:
        if len(tokens) != 1:
            raise CLIUsageError("table clear: requires exactly one table name")
        table = tokens[0]
        try:
            n = switch.client.clear_table(table)  # type: ignore[attr-defined]
        except Exception as exc:
            return _CommandErrorOutput.from_exception(exc)
        return f"cleared {n} entries"

    def _cmd_switch_counter(self, switch_name: str, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError(f"{switch_name} counter: missing counter name (or 'reset')")
        sw = self._network.switch(switch_name)
        if tokens[0] == "reset":
            if len(tokens) < 2:
                raise CLIUsageError(f"{switch_name} counter reset: missing counter name")
            counter = tokens[1]
            index = _parse_optional_int(tokens[2:], label=f"{switch_name} counter reset")
            try:
                if index is None:
                    sw.client.reset_counter(counter)
                else:
                    sw.client.reset_counter(counter, index)
            except Exception as exc:
                return _CommandErrorOutput.from_exception(exc)
            return "ok"
        # read
        counter = tokens[0]
        read_args = tokens[1:]
        nonzero_only = read_args == ["--nonzero"]
        index = (
            None
            if nonzero_only or read_args == ["--all"]
            else _parse_optional_int(read_args, label=f"{switch_name} counter")
        )
        try:
            data = (
                sw.client.read_counter(counter, index)
                if index is not None
                else sw.client.read_counter(counter)
            )
        except Exception as exc:
            return _CommandErrorOutput.from_exception(exc)
        if not isinstance(data, dict):
            return f"pkts={data.packet_count} bytes={data.byte_count}"
        if not data:
            return "(no populated cells)"

        visible_cells = (
            (idx, cell)
            for idx, cell in data.items()
            if not nonzero_only or cell.packet_count != 0 or cell.byte_count != 0
        )
        values = [
            (str(idx), str(cell.packet_count), str(cell.byte_count))
            for idx, cell in sorted(visible_cells)
        ]
        omitted = len(data) - len(values)
        summary = ""
        if omitted:
            cell_label = "cell" if omitted == 1 else "cells"
            summary = f"({omitted} zero {cell_label} omitted. Use --all for the full table.)"
        if not values:
            return summary

        rows = [("index", "pkts", "bytes"), *values]
        index_w = max(len(row[0]) for row in rows)
        pkts_w = max(len(row[1]) for row in rows)
        bytes_w = max(len(row[2]) for row in rows)
        lines = [
            f"{idx:>{index_w}}  {pkts:>{pkts_w}}  {byte_count:>{bytes_w}}"
            for idx, pkts, byte_count in rows
        ]
        lines[0] = bold(lines[0], color=self._color)
        if summary:
            lines.append(summary)
        return "\n".join(lines)

    def _cmd_switch_mcast(self, switch_name: str, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError(f"{switch_name} mcast: missing sub-verb (list/add/del)")
        sub, rest = tokens[0], tokens[1:]
        sw = self._network.switch(switch_name)
        if sub == "list":
            if rest:
                raise CLIUsageError(f"{switch_name} mcast list: takes no arguments")
            try:
                groups = sw.client.list_multicast_groups()
            except Exception as exc:
                return _CommandErrorOutput.from_exception(exc)
            if not groups:
                return "(no multicast groups)"
            lines = []
            for gid in sorted(groups):
                lines.append(f"{gid}: {groups[gid]}")
            return "\n".join(lines)
        if sub == "add":
            if len(rest) != 2:
                raise CLIUsageError(
                    f"{switch_name} mcast add: usage 'mcast add <id> <port>[,<port>...]'"
                )
            try:
                gid = int(rest[0])
            except ValueError as exc:
                raise CLIUsageError(f"{switch_name} mcast add: id must be an integer") from exc
            try:
                ports = [int(p) for p in rest[1].split(",") if p.strip()]
            except ValueError as exc:
                raise CLIUsageError(
                    f"{switch_name} mcast add: ports must be a comma-separated list of integers"
                ) from exc
            try:
                sw.client.add_multicast_group(gid, ports)
            except Exception as exc:
                return _CommandErrorOutput.from_exception(exc)
            return "ok"
        if sub == "del":
            if len(rest) != 1:
                raise CLIUsageError(f"{switch_name} mcast del: usage 'mcast del <id>'")
            try:
                gid = int(rest[0])
            except ValueError as exc:
                raise CLIUsageError(f"{switch_name} mcast del: id must be an integer") from exc
            try:
                sw.client.delete_multicast_group(gid)
            except Exception as exc:
                return _CommandErrorOutput.from_exception(exc)
            return "ok"
        raise CLIUsageError(
            f"{switch_name} mcast: unknown sub-verb {sub!r} (expected one of: list, add, del)"
        )

    def _cmd_switch_packet(self, switch_name: str, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError(f"{switch_name} packet: missing sub-verb (send/listen)")
        sub, rest = tokens[0], tokens[1:]
        sw = self._network.switch(switch_name)
        if sub == "send":
            return self._cmd_switch_packet_send(switch_name, sw, rest)
        if sub == "listen":
            return self._cmd_switch_packet_listen(switch_name, sw, rest)
        raise CLIUsageError(
            f"{switch_name} packet: unknown sub-verb {sub!r} (expected one of: send, listen)"
        )

    def _cmd_switch_packet_send(self, switch_name: str, switch: object, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError(
                f"{switch_name} packet send: missing hex payload "
                "(usage: packet send <hex_payload> [metadata: <k>=<v>[,<k>=<v>...]])"
            )
        hex_payload = tokens[0]
        try:
            payload = bytes.fromhex(hex_payload)
        except ValueError as exc:
            raise CLIUsageError(
                f"{switch_name} packet send: invalid hex payload {hex_payload!r}: {exc}"
            ) from exc
        sections = _parse_sections(tokens[1:], allowed={"metadata"})
        metadata = _parse_kv_pairs(sections["metadata"]) if "metadata" in sections else {}
        try:
            switch.client.send_packet_out(payload, metadata=metadata)  # type: ignore[attr-defined]
        except Exception as exc:
            return _CommandErrorOutput.from_exception(exc)
        return "ok"

    def _cmd_switch_packet_listen(
        self, switch_name: str, switch: RunningSwitch, tokens: list[str]
    ) -> str:
        kv = _parse_kv_pairs(tokens) if tokens else {}
        for k in kv:
            if k not in ("count", "timeout"):
                raise CLIUsageError(
                    f"{switch_name} packet listen: unknown option {k!r} "
                    "(expected count=<int> and/or timeout=<float>)"
                )
        try:
            count = int(kv.get("count", "1"))
        except ValueError as exc:
            raise CLIUsageError(f"{switch_name} packet listen: count must be int") from exc
        try:
            timeout = float(kv.get("timeout", "10.0"))
        except ValueError as exc:
            raise CLIUsageError(f"{switch_name} packet listen: timeout must be float") from exc
        if count <= 0:
            raise CLIUsageError(f"{switch_name} packet listen: count must be positive")

        if not math.isfinite(timeout) or not 0 <= timeout <= threading.TIMEOUT_MAX:
            raise CLIUsageError(
                f"{switch_name} packet listen: timeout must be finite and between "
                f"0 and {threading.TIMEOUT_MAX} seconds"
            )

        progress = self._on_progress
        ready = threading.Condition()
        pending: deque[tuple[bytes, dict[str, int]]] = deque()
        deadline: float | None = None
        accepted = 0

        # The P4Runtime reader only queues packets. The command thread renders them.
        def handler(payload: bytes, metadata: dict[str, int]) -> None:
            nonlocal accepted
            with ready:
                if accepted < count and (deadline is None or monotonic() <= deadline):
                    pending.append((payload, metadata))
                    accepted += 1
                    ready.notify()

        displayed = 0
        lines: list[str] = []
        try:
            with ExitStack() as subscription:
                with defer_cleanup_signals():
                    subscription.callback(switch.client.on_packet_in(handler))
                if timeout > 0 and progress is not None:
                    packet_label = "packet" if count == 1 else "packets"
                    progress(
                        f"Waiting for {count} {packet_label} on {switch_name}"
                        f" (up to {timeout:g}s)..."
                    )
                with ready:
                    deadline = monotonic() + timeout
                while displayed < count:
                    with ready:
                        remaining = max(0.0, min(deadline - monotonic(), threading.TIMEOUT_MAX))
                        if not ready.wait_for(lambda: bool(pending), timeout=remaining):
                            break
                        payload, metadata = pending.popleft()
                    hex_str = payload.hex()
                    if len(hex_str) > 64:
                        hex_str = hex_str[:64] + "..."
                    meta_str = " ".join(f"[{k}={v}]" for k, v in sorted(metadata.items()))
                    line = f"{meta_str} {hex_str}" if meta_str else hex_str
                    if progress is None:
                        lines.append(line)
                    else:
                        progress(line)
                    displayed += 1
        except KeyboardInterrupt:
            if progress is not None:
                progress(f"{displayed}/{count} packets received")
            raise

        if progress is not None:
            suffix = " (timeout)" if displayed < count else ""
            return f"{displayed}/{count} packets received{suffix}"
        return "\n".join(lines) if displayed else f"(no packets within {timeout}s)"

    # ------------------------------------------------------------------
    # Public helpers used by the completer
    # ------------------------------------------------------------------

    def table_names_for(self, switch_name: str) -> list[str]:
        """Return the named switch's table names (for completion)."""
        try:
            sw = self._network.switch(switch_name)
            return list(sw.client.index.table_names)
        except Exception:
            return []

    @staticmethod
    def _format_cmd_result(result: object) -> str:
        out_bytes = getattr(result, "stdout", b"") or b""
        err_bytes = getattr(result, "stderr", b"") or b""
        rc = int(getattr(result, "returncode", 0))
        out = out_bytes.decode("utf-8", errors="replace") if out_bytes else ""
        err = err_bytes.decode("utf-8", errors="replace") if err_bytes else ""
        parts: list[str] = []
        if out:
            parts.append(out.rstrip("\n"))
        if err:
            for line in err.rstrip("\n").splitlines():
                parts.append(f"[stderr] {line}")
        if rc != 0:
            parts.append(f"[exit {rc}]")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Table dump rendering
# ---------------------------------------------------------------------------


def _format_table_fields(label: str, fields: Mapping[str, str]) -> list[str]:
    prefix = f"  {label + ':':<9} "
    if not fields:
        return [prefix + "(no fields)"]
    first, *remaining = (f"{name}={value}" for name, value in fields.items())
    continuation = " " * len(prefix)
    return [prefix + first, *(continuation + field for field in remaining)]


def _render_action_params(
    index: object,
    action_name: object,
    params: dict[str, bytes],
) -> dict[str, str]:
    """Decode raw action-param bytes into width-aware human strings.

    The ``index`` argument MUST be the ``P4InfoIndex`` of the switch the
    table-dump invocation is targeting; never a globally-cached or
    first-found index. This is what allows multi-switch topologies where two
    switches share an action name to render each switch's params correctly.

    Falls back to ``repr(bytes)`` for any param whose width can't be looked up.
    """
    from p4net.control.codec import format_exact

    if not isinstance(action_name, str):
        return {k: repr(v) for k, v in params.items()}
    try:
        for action_msg in index.raw.actions:  # type: ignore[attr-defined]
            if action_msg.preamble.name == action_name:
                bw_by_name = {p.name: int(p.bitwidth) for p in action_msg.params}
                out: dict[str, str] = {}
                for k, v in params.items():
                    bw = bw_by_name.get(k)
                    out[k] = format_exact(v, bw) if bw else repr(v)
                return out
    except Exception as exc:
        _log.debug("decode action params failed for %s: %s", action_name, exc)
    return {k: repr(v) for k, v in params.items()}


# ---------------------------------------------------------------------------
# Section / kv parsing helpers (for `<switch> table add` etc.)
# ---------------------------------------------------------------------------


def _parse_table_match(
    index: P4InfoIndex, table_name: str, values: dict[str, str]
) -> dict[str, object]:
    """Convert composite match values using the target switch's field types."""
    kinds = {
        field.name: _match_type_label(field.match_type)
        for table in index.raw.tables
        if table.preamble.name == table_name
        for field in table.match_fields
    }
    formats = {"ternary": ("&&&", "value&&&mask"), "range": ("..", "low..high")}
    result: dict[str, object] = {}
    for name, value in values.items():
        kind = kinds.get(name)
        if kind not in formats:
            result[name] = value
            continue
        separator, usage = formats[kind]
        parts = [part.strip() for part in value.split(separator)]
        if len(parts) != 2 or not all(parts):
            raise CLIUsageError(
                f"table {table_name!r} match field {name!r}: expected {usage}, got {value!r}"
            )
        result[name] = (parts[0], parts[1])
    return result


def _parse_sections(tokens: list[str], *, allowed: set[str]) -> dict[str, list[str]]:
    """Walk tokens, splitting on section markers (`match:`, `action:`, ...).

    A token of the shape `<name>:` outside an assignment starts a section.
    Unknown names raise `CLIUsageError`. Assignment values may end in a
    colon, as in IPv6 addresses.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for tok in tokens:
        if tok.endswith(":") and len(tok) > 1 and "=" not in tok:
            name = tok[:-1]
            if name not in allowed:
                raise CLIUsageError(
                    f"unknown section marker {tok!r} (expected one of: {sorted(allowed)})"
                )
            if name in sections:
                raise CLIUsageError(f"section {name!r}: appears twice in command")
            current = name
            sections[current] = []
            continue
        if current is None:
            raise CLIUsageError(
                f"unexpected token before any section marker: {tok!r} "
                f"(expected one of: {sorted(allowed)})"
            )
        sections[current].append(tok)
    for name, vals in list(sections.items()):
        if not vals and name in {"match", "params"}:
            raise CLIUsageError(f"section {name!r}: empty")
    return sections


def _parse_kv_pairs(tokens: list[str]) -> dict[str, str]:
    """Parse `a=1, b=2` (with optional spaces) or `a=1,b=2` into a dict."""
    joined = ",".join(tokens)
    out: dict[str, str] = {}
    for piece in joined.split(","):
        item = piece.strip()
        if not item:
            continue
        if "=" not in item:
            raise CLIUsageError(f"expected key=value, got {item!r}")
        k, _, v = item.partition("=")
        key = k.strip()
        value = v.strip()
        if not key:
            raise CLIUsageError(f"empty key in {item!r}")
        out[key] = value
    if not out:
        raise CLIUsageError("expected at least one key=value pair")
    return out


def _parse_priority(tokens: list[str] | None) -> int | None:
    if tokens is None:
        return None
    if len(tokens) != 1:
        raise CLIUsageError(f"priority: must be a single integer, got {tokens!r}")
    try:
        return int(tokens[0])
    except ValueError as exc:
        raise CLIUsageError(f"priority: not an integer: {tokens[0]!r}") from exc


def _parse_optional_int(tokens: list[str], *, label: str) -> int | None:
    if not tokens:
        return None
    if len(tokens) > 1:
        raise CLIUsageError(f"{label}: too many arguments")
    try:
        return int(tokens[0])
    except ValueError as exc:
        raise CLIUsageError(f"{label}: index must be an integer, got {tokens[0]!r}") from exc


_MATCH_TYPE_LABELS = {
    2: "exact",
    3: "lpm",
    4: "ternary",
    5: "range",
    6: "optional",
}


def _match_type_label(value: int) -> str:
    return _MATCH_TYPE_LABELS.get(int(value), f"type{value}")
