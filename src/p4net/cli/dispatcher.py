"""Pure command parser/executor for the p4net interactive shell.

The dispatcher takes a `Network`, accepts a single input line, and returns
formatted output (strings only). It carries no interactive concerns: the
shell wraps it, and unit tests target it directly with a mock Network.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING

from p4net.cli.exceptions import CLIExit, CLIUsageError
from p4net.cli.formatting import bold, render_pingall_matrix

if TYPE_CHECKING:
    from p4net.network import Network

# A switch verb handler takes (switch_name, remaining tokens) and returns
# the formatted output string.
SwitchHandler = Callable[[str, list[str]], str]


# Top-level help registry. The first column is the topic key (also the
# completer entry); the second is a short description; the third is the
# detailed usage line.
_TOPIC_HELP: dict[str, tuple[str, str]] = {
    "help": ("List commands or show details for one.", "help [command]"),
    "exit": ("Exit the shell.", "exit"),
    "quit": ("Exit the shell.", "quit"),
    "status": ("Print network status (running, counts, log dir).", "status"),
    "hosts": ("List hosts and their primary IPs / interfaces.", "hosts"),
    "switches": ("List switches and their gRPC addresses / pids.", "switches"),
    "pingall": (
        "Ping every pair of hosts; print a result matrix.",
        "pingall [count] [timeout]",
    ),
    "<host> ping": (
        "Ping a target from a host.",
        "<host> ping <target> [count] [timeout]",
    ),
    "<host> cmd": (
        "Run a command inside a host's namespace.",
        "<host> cmd <argv ...>",
    ),
    "<host> ifconfig": (
        "Show host interfaces (ip -br addr).",
        "<host> ifconfig",
    ),
}


class CommandDispatcher:
    """Parse and execute p4net CLI commands against a running Network."""

    def __init__(self, network: Network, *, color: bool = False) -> None:
        self._network = network
        self._color = color
        self._top_level_handlers = {
            "help": self._cmd_help,
            "exit": self._cmd_exit,
            "quit": self._cmd_exit,
            "status": self._cmd_status,
            "hosts": self._cmd_hosts,
            "switches": self._cmd_switches,
            "pingall": self._cmd_pingall,
        }
        self._host_handlers = {
            "ping": self._cmd_host_ping,
            "cmd": self._cmd_host_cmd,
            "ifconfig": self._cmd_host_ifconfig,
        }
        # Switch handlers populated in commit 3.
        self._switch_handlers: dict[str, SwitchHandler] = {}
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
        return list(self._top_level_handlers.keys())

    @property
    def host_names(self) -> list[str]:
        return list(self._network.hosts.keys())

    @property
    def switch_names(self) -> list[str]:
        return list(self._network.switches.keys())

    @property
    def color(self) -> bool:
        return self._color

    # ------------------------------------------------------------------
    # Internal: top-level commands
    # ------------------------------------------------------------------

    def _cmd_help(self, tokens: list[str]) -> str:
        if not tokens:
            lines = [bold("Commands", color=self._color)]
            for topic, (desc, _usage) in self._help_topics.items():
                lines.append(f"  {topic:<22} {desc}")
            return "\n".join(lines)
        topic = " ".join(tokens)
        info = self._help_topics.get(topic)
        if info is None:
            raise CLIUsageError(f"no help for {topic!r}")
        desc, usage = info
        return f"{usage}\n\n{desc}"

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
        rows: list[tuple[str, str, str]] = []
        for name, host in hosts.items():
            ip = host.primary_ip or "-"
            ifaces = ", ".join(host.interfaces) if host.interfaces else "-"
            rows.append((name, ip, ifaces))
        name_w = max(4, max(len(r[0]) for r in rows))
        ip_w = max(10, max(len(r[1]) for r in rows))
        header = bold(
            f"{'name'.ljust(name_w)}  {'primary_ip'.ljust(ip_w)}  interfaces",
            color=self._color,
        )
        lines = [header]
        for name, ip, ifaces in rows:
            lines.append(f"{name.ljust(name_w)}  {ip.ljust(ip_w)}  {ifaces}")
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

    def _cmd_pingall(self, tokens: list[str]) -> str:
        count, timeout = self._parse_count_timeout(tokens, label="pingall")
        result = self._network.pingall(count=count, timeout=timeout)
        hosts = list(self._network.hosts.keys())
        return render_pingall_matrix(hosts, result, color=self._color)

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

    def _cmd_host_ping(self, host_name: str, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError(f"{host_name} ping: missing target")
        target = tokens[0]
        count, timeout = self._parse_count_timeout(tokens[1:], label=f"{host_name} ping")
        host = self._network.host(host_name)
        # If the target is a known host name, look up its primary_ip.
        # Otherwise, pass the string through to ping (treated as a literal IP).
        target_host = self._network.hosts.get(target)
        if target_host is not None:
            if target_host.primary_ip is None:
                raise CLIUsageError(f"target {target!r}: no primary IP configured")
            ok = host.ping(target_host, count=count, timeout=timeout)
        else:
            ok = host.ping(target, count=count, timeout=timeout)
        return "OK" if ok else "FAIL"

    def _cmd_host_cmd(self, host_name: str, tokens: list[str]) -> str:
        if not tokens:
            raise CLIUsageError(f"{host_name} cmd: missing argv")
        host = self._network.host(host_name)
        result = host.exec(tokens, timeout=30, check=False, capture_output=True)
        return self._format_cmd_result(result)

    def _cmd_host_ifconfig(self, host_name: str, tokens: list[str]) -> str:
        if tokens:
            raise CLIUsageError(f"{host_name} ifconfig: takes no arguments")
        host = self._network.host(host_name)
        result = host.exec(["ip", "-br", "addr"], check=False, capture_output=True)
        return result.stdout.decode("utf-8", errors="replace").rstrip("\n")

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
        return count, timeout

    @staticmethod
    def _format_cmd_result(result: object) -> str:
        # `result` is a subprocess.CompletedProcess[bytes]; we don't import
        # subprocess here to keep the dispatcher type-light.
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
