"""Process-wide cleanup hooks for `Network` instances.

A module-level registry of active `Network` objects, plus an `atexit` hook
and (only on the main thread) SIGINT/SIGTERM handlers that call
`net.stop()` on each before re-raising the signal via the previously
installed handler. The atexit hook is installed once. Signal handlers can
be installed later on the main thread if a worker installed the exit hook.
In an event loop, signal cleanup awaits `astop()` before redelivery.

Forked children start with their own empty registry and an unlocked mutex.

The signal handler does NOT call `sys.exit` directly. After running
cleanup it restores the previous handler and re-delivers the signal via
`os.kill(os.getpid(), signum)`, so the parent process / interactive shell
sees canonical termination behavior.
"""

from __future__ import annotations

import asyncio
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
_SIGNAL_LOOPS: dict[int, asyncio.AbstractEventLoop] = {}
_SIGNAL_TASKS: dict[int, asyncio.Task[None] | None] = {}
_LOCK = threading.Lock()


def _reset_after_fork() -> None:
    """Discard parent-owned networks and the inherited registry lock."""
    global _ACTIVE, _LOCK, _SIGNAL_LOOPS, _SIGNAL_TASKS
    _ACTIVE = set()
    _LOCK = threading.Lock()
    for sig, loop in _SIGNAL_LOOPS.items():
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            loop.remove_signal_handler(sig)
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _make_handler(sig))
    _SIGNAL_LOOPS = {}
    _SIGNAL_TASKS = {}


os.register_at_fork(after_in_child=_reset_after_fork)


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
    """Install the exit hook once and retry missing signals on the main thread."""
    global _INSTALLED
    with _LOCK:
        if not _INSTALLED:
            atexit.register(_run_cleanup)
            _INSTALLED = True
    if threading.current_thread() is threading.main_thread():
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        for sig in (signal.SIGINT, signal.SIGTERM):
            owner = _SIGNAL_LOOPS.get(sig)
            if owner is not None and owner.is_closed():
                _SIGNAL_LOOPS.pop(sig)
                _PREV_HANDLERS.pop(sig, None)
                owner = None
            if sig in _PREV_HANDLERS and owner is loop:
                continue
            previous = _PREV_HANDLERS.get(sig, signal.getsignal(sig))
            try:
                if owner is not None:
                    owner.remove_signal_handler(sig)
                    signal.signal(sig, previous)
                    _SIGNAL_LOOPS.pop(sig, None)
                _PREV_HANDLERS[sig] = previous
                if loop is None:
                    signal.signal(sig, _make_handler(sig))
                else:
                    # asyncio's wakeup fd also wakes the loop when a worker receives a signal.
                    loop.add_signal_handler(sig, _make_handler(sig), sig, None)
                    _SIGNAL_LOOPS[sig] = loop
            except (ValueError, OSError, RuntimeError) as exc:
                if sig not in _SIGNAL_LOOPS:
                    if loop is not None:
                        try:
                            signal.signal(sig, _make_handler(sig))
                        except (ValueError, OSError):
                            _PREV_HANDLERS.pop(sig, None)
                    else:
                        _PREV_HANDLERS.pop(sig, None)
                logger.debug("cleanup: failed to install handler for %s: %r", sig, exc)


def _redeliver(sig: int) -> None:
    prev = _PREV_HANDLERS.pop(sig, None)
    loop = _SIGNAL_LOOPS.pop(sig, None)
    if loop is not None:
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            loop.remove_signal_handler(sig)
    with contextlib.suppress(ValueError, OSError):
        signal.signal(sig, prev if prev is not None else signal.SIG_DFL)
    with contextlib.suppress(OSError):
        os.kill(os.getpid(), sig)


async def _cleanup_before_signal(sig: int) -> None:
    try:
        with _LOCK:
            snapshot = list(_ACTIVE)
        for net in snapshot:
            try:
                await net.astop()
            except asyncio.CancelledError:
                # astop completes its cleanup before propagating cancellation.
                continue
            except Exception as exc:
                logger.debug("cleanup: %r.astop() raised %r", net, exc)
    finally:
        try:
            _redeliver(sig)
        finally:
            _SIGNAL_TASKS.pop(sig, None)


async def wait_for_signal_cleanup() -> None:
    """Keep a failing startup alive until pending signal cleanup can redeliver."""
    if threading.current_thread() is not threading.main_thread():
        return
    loop = asyncio.get_running_loop()
    while any(task is None for task in _SIGNAL_TASKS.values()):
        await asyncio.sleep(0)
    for task in list(_SIGNAL_TASKS.values()):
        if task is not None and task.get_loop() is loop:
            await asyncio.shield(task)


def _start_signal_cleanup(loop: asyncio.AbstractEventLoop, sig: int) -> None:
    _SIGNAL_TASKS[sig] = loop.create_task(_cleanup_before_signal(sig))


def _make_handler(sig: int) -> Any:
    def _handler(signum: int, frame: Any) -> None:
        if sig in _SIGNAL_TASKS:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _run_cleanup()
            _redeliver(sig)
        else:
            _SIGNAL_TASKS[sig] = None
            loop.call_soon_threadsafe(_start_signal_cleanup, loop, sig)

    return _handler
