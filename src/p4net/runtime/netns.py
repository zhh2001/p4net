"""Linux network namespace primitive backed by pyroute2.

`NetworkNamespace` is a thin lifecycle wrapper around `/var/run/netns/<name>`.
All operations talk to netlink via pyroute2 (`pyroute2.netns`, `pyroute2.NSPopen`)
rather than shelling out to `ip netns`.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import IO, Any

from pyroute2 import NSPopen
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
        return self._name

    @property
    def exists(self) -> bool:
        return self._name in _netns.listnetns()

    def create(self) -> None:
        if self.exists:
            raise NamespaceError(f"namespace {self._name!r} already exists")
        _netns.create(self._name)
        logger.debug("created network namespace %r", self._name)

    def destroy(self) -> None:
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
        kwargs: dict[str, Any] = {}
        if env is not None:
            kwargs["env"] = dict(env)
        if capture_output:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
        proc = NSPopen(self._name, list(argv), **kwargs)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
            returncode = proc.returncode
        finally:
            proc.release()
        result: subprocess.CompletedProcess[bytes] = subprocess.CompletedProcess(
            args=list(argv),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if check and returncode != 0:
            raise subprocess.CalledProcessError(
                returncode, list(argv), output=stdout, stderr=stderr
            )
        return result

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

        Returns an `NSProcess` wrapping `pyroute2.NSPopen`. The wrapper
        exposes a `subprocess.Popen`-compatible surface and ensures the
        underlying namespace handle is released on context-manager exit,
        on `close()`, or as a last resort during garbage collection.
        """
        kwargs: dict[str, Any] = {}
        if env is not None:
            kwargs["env"] = dict(env)
        if stdout is not None:
            kwargs["stdout"] = stdout
        if stderr is not None:
            kwargs["stderr"] = stderr
        if stdin is not None:
            kwargs["stdin"] = stdin
        popen = NSPopen(self._name, list(argv), **kwargs)
        logger.debug(
            "spawned process %r in namespace %r (pid=%d)", list(argv), self._name, popen.pid
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

    Wraps `pyroute2.NSPopen` and ensures the underlying namespace handle is
    released on context-manager exit, on `close()`, and as a last resort
    during garbage collection. The forwarded API mirrors `subprocess.Popen`:
    `pid`, `poll()`, `wait(timeout=None)`, `terminate()`, `kill()`.
    """

    def __init__(self, popen: Any) -> None:
        self._popen = popen
        self._closed = False

    @property
    def pid(self) -> int:
        return int(self._popen.pid)

    def poll(self) -> int | None:
        rc = self._popen.poll()
        return None if rc is None else int(rc)

    def wait(self, timeout: float | None = None) -> int:
        return int(self._popen.wait(timeout=timeout))

    def terminate(self) -> None:
        self._popen.terminate()

    def kill(self) -> None:
        self._popen.kill()

    def close(self) -> None:
        """Release the underlying NSPopen helper. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._popen.release()
        except Exception as exc:
            logger.debug("NSProcess.close: release() raised: %r", exc)

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
            try:
                self.close()
            except Exception as cerr:
                logger.debug("NSProcess.__exit__: close failed: %r", cerr)

    def __del__(self) -> None:
        # __del__ must never raise; swallow everything.
        with contextlib.suppress(Exception):
            self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"NSProcess(pid={self.pid if not self._closed else '?'}, {state})"
