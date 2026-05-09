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
from p4net.network.exceptions import NetworkError
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
    ) -> None:
        self._host = host
        self._namespace = namespace
        self._interfaces: dict[str, str | None] = dict(interfaces)

    @property
    def name(self) -> str:
        return self._host.name

    @property
    def descriptor(self) -> Host:
        return self._host

    @property
    def namespace(self) -> NetworkNamespace:
        return self._namespace

    @property
    def interfaces(self) -> Mapping[str, str | None]:
        return self._interfaces

    @property
    def primary_ip(self) -> str | None:
        """Address of the first configured interface (without /mask), or None."""
        for cidr in self._interfaces.values():
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
        return self._namespace.popen(argv, env=env, stdout=stdout, stderr=stderr, stdin=stdin)

    def ping(
        self,
        dst: str | RunningHost,
        *,
        count: int = 1,
        timeout: float = 2.0,
    ) -> bool:
        """Run `ping -c <count> -W <int(timeout)>` and return whether it succeeded."""
        if isinstance(dst, RunningHost):
            target = dst.primary_ip
            if target is None:
                raise NetworkError(f"cannot ping host {dst.name!r}: no primary IP configured")
        else:
            target = dst
        result = self._namespace.exec(
            ["ping", "-c", str(int(count)), "-W", str(int(timeout)), target],
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
        return self._switch.name

    @property
    def descriptor(self) -> P4Switch:
        return self._switch

    @property
    def bmv2(self) -> BMv2Switch:
        return self._bmv2

    @property
    def client(self) -> P4RuntimeClient:
        return self._client

    @property
    def compile_result(self) -> CompileResult:
        return self._compile_result

    @property
    def log_file(self) -> Path:
        return self._bmv2.log_file

    def __repr__(self) -> str:
        return f"RunningSwitch(name={self.name!r}, grpc={self._bmv2.grpc_address!r})"


__all__ = ["RunningHost", "RunningSwitch"]
