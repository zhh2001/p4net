"""Linux network namespace primitive.

`NetworkNamespace` is a thin lifecycle wrapper around `/var/run/netns/<name>`.
Lifecycle (`create`/`destroy`/`exists`) talks to netlink via pyroute2's
`netns` module. In-namespace command execution (`exec`/`popen`) shells out
to the standard `ip netns exec` wrapper rather than `pyroute2.NSPopen`:
`NSPopen` forks a Python helper before `setns()`, which deadlocks under any
multi-threaded Python parent (e.g. pytest after a `P4RuntimeClient` has
opened its bidirectional stream). `subprocess.Popen` of `ip netns exec`
forks-and-execs straight into the iproute2 binary, bypassing the unsafe
fork+Python window entirely.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import IO, Any

from pyroute2 import netns as _netns

from p4net.runtime.exceptions import NamespaceError

logger = logging.getLogger(__name__)

_NAME_MAX_LEN = 32


def _validate_name(name: str) -> None:
    if not isinstance(name, str):
        raise ValueError("namespace name must be a string")
    if not name:
        raise ValueError("namespace name must be non-empty")
    if any(ch.isspace() for ch in name):
        raise ValueError("namespace name must not contain whitespace")
    if "/" in name:
        raise ValueError("namespace name must not contain '/'")
    if ".." in name:
        raise ValueError("namespace name must not contain '..'")
    if len(name) > _NAME_MAX_LEN:
        raise ValueError(f"namespace name longer than {_NAME_MAX_LEN} characters")


class NetworkNamespace:
    """A named Linux network namespace with explicit lifecycle.

    Use `create()` / `destroy()` directly, or use the instance as a context
    manager. `exec()` runs a command inside the namespace and waits for it;
    `popen()` returns a long-running process handle.
    """

    def __init__(self, name: str) -> None:
        _validate_name(name)
        self._name = name

    @property
    def name(self) -> str:
        """The kernel-visible namespace name."""
        return self._name

    @property
    def exists(self) -> bool:
        """``True`` while the namespace is present in ``ip netns list``."""
        return self._name in _netns.listnetns()

    def create(self) -> None:
        """Create the namespace.

        Raises:
            NamespaceError: if a namespace with the same name already exists.
        """
        if self.exists:
            raise NamespaceError(f"namespace {self._name!r} already exists")
        _netns.create(self._name)
        logger.debug("created network namespace %r", self._name)

    def destroy(self) -> None:
        """Remove the namespace.

        Raises:
            NamespaceError: if the namespace does not exist.
        """
        if not self.exists:
            raise NamespaceError(f"namespace {self._name!r} does not exist")
        _netns.remove(self._name)
        logger.debug("destroyed network namespace %r", self._name)

    def exec(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        check: bool = True,
        capture_output: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run `argv` inside this namespace and wait for completion.

        Mirrors `subprocess.run` semantics: returns a `CompletedProcess`,
        raises `subprocess.CalledProcessError` if `check` and the exit
        status is non-zero, raises `subprocess.TimeoutExpired` on timeout.
        """
        full_argv = ["ip", "netns", "exec", self._name, *argv]
        return subprocess.run(
            full_argv,
            timeout=timeout,
            check=check,
            capture_output=capture_output,
            env=dict(env) if env is not None else None,
        )

    def popen(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdout: int | IO[Any] | None = None,
        stderr: int | IO[Any] | None = None,
        stdin: int | IO[Any] | None = None,
    ) -> NSProcess:
        """Spawn a long-running process inside this namespace.

        Returns an `NSProcess` wrapping a regular `subprocess.Popen` of
        `ip netns exec <name> <argv>`. The wrapper exposes a
        `subprocess.Popen`-compatible surface (`pid`, `poll`, `wait`,
        `terminate`, `kill`, `close`).
        """
        full_argv = ["ip", "netns", "exec", self._name, *argv]
        kwargs: dict[str, Any] = {}
        if env is not None:
            kwargs["env"] = dict(env)
        if stdout is not None:
            kwargs["stdout"] = stdout
        if stderr is not None:
            kwargs["stderr"] = stderr
        if stdin is not None:
            kwargs["stdin"] = stdin
        popen = subprocess.Popen(full_argv, **kwargs)
        logger.debug(
            "spawned process %r in namespace %r (pid=%d)",
            list(argv),
            self._name,
            popen.pid,
        )
        return NSProcess(popen)

    def __enter__(self) -> NetworkNamespace:
        self.create()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.exists:
            self.destroy()

    def __repr__(self) -> str:
        return f"NetworkNamespace({self._name!r})"


class NSProcess:
    """A process running inside a `NetworkNamespace`.

    Wraps a regular `subprocess.Popen` returned by `NetworkNamespace.popen`.
    The forwarded API (`pid`, `poll`, `wait`, `terminate`, `kill`) mirrors
    `subprocess.Popen`. `close()` is preserved as a no-op so callers from
    earlier phases keep working unchanged: there is no namespace-side
    handle to release because the wrapped process is a regular child of
    `ip netns exec`.
    """

    def __init__(self, popen: Any) -> None:
        self._popen = popen
        self._closed = False

    @property
    def pid(self) -> int:
        """OS process ID of the wrapped child."""
        return int(self._popen.pid)

    def poll(self) -> int | None:
        """Non-blocking liveness check; returns the exit code or ``None``."""
        rc = self._popen.poll()
        return None if rc is None else int(rc)

    def wait(self, timeout: float | None = None) -> int:
        """Block until the child exits and return its exit code."""
        return int(self._popen.wait(timeout=timeout))

    def terminate(self) -> None:
        """Send ``SIGTERM`` to the child."""
        self._popen.terminate()

    def kill(self) -> None:
        """Send ``SIGKILL`` to the child."""
        self._popen.kill()

    def close(self) -> None:
        """Idempotent no-op preserved for API stability.

        Earlier phases needed this method to release a `pyroute2.NSPopen`
        helper; with a regular `subprocess.Popen` there is nothing to
        release, but the method is kept so callers compiled against the
        old API don't break. Calling `close` more than once is safe.
        """
        self._closed = True

    def __enter__(self) -> NSProcess:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self.poll() is None:
                try:
                    self.terminate()
                    self.wait(timeout=5)
                except Exception as terr:
                    logger.debug("NSProcess.__exit__: terminate/wait failed: %r", terr)
                if self.poll() is None:
                    try:
                        self.kill()
                        self.wait()
                    except Exception as kerr:
                        logger.debug("NSProcess.__exit__: kill/wait failed: %r", kerr)
        finally:
            self.close()

    def __del__(self) -> None:
        # __del__ must never raise; swallow everything.
        with contextlib.suppress(Exception):
            self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"NSProcess(pid={self.pid if not self._closed else '?'}, {state})"
