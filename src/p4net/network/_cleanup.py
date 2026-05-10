"""Process-wide cleanup hooks for `Network` instances.

A module-level registry of active `Network` objects, plus an `atexit` hook
and (only on the main thread) SIGINT/SIGTERM handlers that call
`net.stop()` on each before re-raising the signal via the previously
installed handler. Idempotent installation: a single `_INSTALLED` flag
prevents double-registration.

The signal handler does NOT call `sys.exit` directly. After running
cleanup it restores the previous handler and re-delivers the signal via
`os.kill(os.getpid(), signum)`, so the parent process / interactive shell
sees canonical termination behavior.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import signal
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from p4net.network.orchestrator import Network

logger = logging.getLogger(__name__)

_ACTIVE: set[Network] = set()
_INSTALLED = False
_PREV_HANDLERS: dict[int, Any] = {}
_LOCK = threading.Lock()


def register(net: Network) -> None:
    """Add ``net`` to the cleanup registry so atexit / signal handlers stop it."""
    with _LOCK:
        _ACTIVE.add(net)


def unregister(net: Network) -> None:
    """Remove ``net`` from the cleanup registry. Idempotent."""
    with _LOCK:
        _ACTIVE.discard(net)


def _run_cleanup() -> None:
    with _LOCK:
        snapshot = list(_ACTIVE)
    for net in snapshot:
        try:
            net.stop()
        except Exception as exc:
            logger.debug("cleanup: %r.stop() raised %r", net, exc)


def install_handlers() -> None:
    """Install atexit + SIGINT/SIGTERM hooks. Idempotent."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        _INSTALLED = True
    atexit.register(_run_cleanup)
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                prev = signal.signal(sig, _make_handler(sig))
                _PREV_HANDLERS[sig] = prev
            except (ValueError, OSError) as exc:
                # On some embedded interpreters signal.signal can fail; log and continue.
                logger.debug("cleanup: failed to install handler for %s: %r", sig, exc)


def _make_handler(sig: int) -> Any:
    def _handler(signum: int, frame: Any) -> None:
        _run_cleanup()
        prev = _PREV_HANDLERS.pop(signum, None)
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signum, prev if prev is not None else signal.SIG_DFL)
        # Re-deliver the signal so the parent process / shell sees canonical behavior.
        with contextlib.suppress(OSError):
            os.kill(os.getpid(), signum)

    return _handler
