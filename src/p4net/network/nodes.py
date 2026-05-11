"""Read-only handles to running hosts and switches.

Both classes are pure data + thin convenience wrappers. They do NOT manage
lifecycle — the `Network` orchestrator does. They expose enough of the
underlying primitives (namespace, BMv2 process, P4Runtime client) that test
code and applications can do interesting things without reaching into the
orchestrator's internals.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

from p4net.compiler import CompileResult
from p4net.control import P4RuntimeClient
from p4net.network.exceptions import NetworkError, NetworkNotRunningError
from p4net.runtime import BMv2Switch, NetworkNamespace, NSProcess
from p4net.topo import Host, P4Switch


def _strip_mask(cidr: str) -> str:
    return cidr.split("/", 1)[0] if "/" in cidr else cidr


class RunningHost:
    """A host that has been brought up and is ready to run commands."""

    def __init__(
        self,
        host: Host,
        namespace: NetworkNamespace,
        interfaces: Mapping[str, str | None],
        interfaces6: Mapping[str, str | None] | None = None,
    ) -> None:
        self._host = host
        self._namespace = namespace
        self._interfaces: dict[str, str | None] = dict(interfaces)
        self._interfaces6: dict[str, str | None] = dict(interfaces6 or {})

    @property
    def name(self) -> str:
        """The host's name as declared in the topology."""
        return self._host.name

    @property
    def descriptor(self) -> Host:
        """The :class:`Host` topology descriptor that produced this runtime."""
        return self._host

    @property
    def namespace(self) -> NetworkNamespace:
        """The Linux network namespace this host runs in."""
        return self._namespace

    @property
    def interfaces(self) -> Mapping[str, str | None]:
        """Map of interface name → IPv4 CIDR (or ``None``) for this host."""
        return self._interfaces

    @property
    def interfaces6(self) -> Mapping[str, str | None]:
        """Map of interface name → IPv6 CIDR (or ``None``) for this host."""
        return self._interfaces6

    @property
    def primary_ip(self) -> str | None:
        """Address of the first configured IPv4 interface (without /mask), or None."""
        for cidr in self._interfaces.values():
            if cidr:
                return _strip_mask(cidr)
        return None

    @property
    def primary_ip6(self) -> str | None:
        """Address of the first configured IPv6 interface (without /mask), or None."""
        for cidr in self._interfaces6.values():
            if cidr:
                return _strip_mask(cidr)
        return None

    def exec(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        check: bool = True,
        capture_output: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run ``argv`` synchronously inside the host's namespace.

        Args:
            argv: Command and arguments to execute.
            timeout: Per-call timeout in seconds.
            check: Raise :class:`subprocess.CalledProcessError` on rc != 0.
            capture_output: Capture stdout/stderr instead of inheriting.
            env: Optional environment variable overrides.

        Returns:
            The completed :class:`subprocess.CompletedProcess`.
        """
        return self._namespace.exec(
            argv,
            timeout=timeout,
            check=check,
            capture_output=capture_output,
            env=env,
        )

    def popen(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdout: int | IO[bytes] | None = None,
        stderr: int | IO[bytes] | None = None,
        stdin: int | IO[bytes] | None = None,
    ) -> NSProcess:
        """Spawn a long-running process inside the host's namespace.

        Returns an :class:`NSProcess` whose lifecycle the caller manages.
        """
        return self._namespace.popen(argv, env=env, stdout=stdout, stderr=stderr, stdin=stdin)

    def ping(
        self,
        dst: str | RunningHost,
        *,
        count: int = 1,
        timeout: float = 2.0,
        force_ipv6: bool = False,
    ) -> bool:
        """Run ``ping`` and return whether at least one reply arrived.

        Auto-selects IPv4 vs IPv6 based on the target string (``:`` → IPv6),
        or pass ``force_ipv6=True`` to force the IPv6 path. When ``dst`` is a
        :class:`RunningHost`, the IPv4 primary is preferred (least surprise
        for existing callers); pass ``force_ipv6=True`` plus a string target
        if you need IPv6 explicitly.
        """
        if isinstance(dst, RunningHost):
            target = dst.primary_ip if not force_ipv6 else dst.primary_ip6
            if target is None and not force_ipv6:
                target = dst.primary_ip6  # v4-less host falls through to v6
            if target is None:
                raise NetworkError(f"cannot ping host {dst.name!r}: no primary IP configured")
        else:
            target = dst
        is_v6 = force_ipv6 or ":" in target
        # `-W` is a per-reply timeout. `-w` enforces an overall deadline so
        # the command terminates even when every echo request goes unanswered
        # (e.g. under 100%-loss netem); without it iputils-ping can hang
        # indefinitely waiting for the last reply.
        deadline = max(int(count) * int(timeout) + 1, int(timeout) + 1)
        result = self._namespace.exec(
            [
                "ping",
                "-6" if is_v6 else "-4",
                "-c",
                str(int(count)),
                "-W",
                str(int(timeout)),
                "-w",
                str(deadline),
                target,
            ],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def __repr__(self) -> str:
        return f"RunningHost(name={self.name!r}, primary_ip={self.primary_ip!r})"


class RunningSwitch:
    """A switch whose BMv2 process is up and whose P4Runtime client is connected."""

    def __init__(
        self,
        switch: P4Switch,
        bmv2: BMv2Switch,
        client: P4RuntimeClient,
        compile_result: CompileResult,
    ) -> None:
        self._switch = switch
        self._bmv2 = bmv2
        self._client = client
        self._compile_result = compile_result

    @property
    def name(self) -> str:
        """The switch's name as declared in the topology."""
        return self._switch.name

    @property
    def descriptor(self) -> P4Switch:
        """The :class:`P4Switch` topology descriptor that produced this runtime."""
        return self._switch

    @property
    def bmv2(self) -> BMv2Switch:
        """The wrapped BMv2 process (PID, gRPC address, log file)."""
        return self._bmv2

    @property
    def client(self) -> P4RuntimeClient:
        """The P4Runtime gRPC client connected to this switch."""
        return self._client

    @property
    def compile_result(self) -> CompileResult:
        """The compiler output (BMv2 JSON + P4Info paths) used by this switch."""
        return self._compile_result

    @property
    def log_file(self) -> Path:
        """Path to the BMv2 log file."""
        return self._bmv2.log_file

    @property
    def boot_timestamp_us(self) -> int:
        """Wall-clock microseconds since Unix epoch when this switch's BMv2
        process started.

        Combined with INT shim ``ingress_timestamp_us``, gives wall-clock
        arrival time::

            wall_clock_us = switch.boot_timestamp_us + shim.ingress_timestamp_us

        This is the alignment point for comparing timestamps across multiple
        switches — BMv2's per-process ``ingress_global_timestamp`` clock
        starts at zero on each process's boot.

        Raises:
            NetworkNotRunningError: if the underlying BMv2 process has not
                been started (or has already been stopped).
        """
        ts = self._bmv2.boot_timestamp_us
        if ts is None:
            raise NetworkNotRunningError(
                f"switch {self.name!r} has no boot timestamp; BMv2 is not running"
            )
        return ts

    def __repr__(self) -> str:
        return f"RunningSwitch(name={self.name!r}, grpc={self._bmv2.grpc_address!r})"


__all__ = ["RunningHost", "RunningSwitch"]
