"""Process isolation for network cleanup hooks."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_FORK_SCRIPT = """
import os
import select
import signal
import sys
import time
import traceback
from pathlib import Path

from p4net.network import _cleanup as cleanup

mode, log_name = sys.argv[1:]
log = Path(log_name)


class TrackedNetwork:
    def __init__(self, name):
        self.name = name

    def stop(self):
        with log.open("a") as output:
            output.write(f"{self.name}:{os.getpid()}\\n")


parent = TrackedNetwork("parent")
cleanup.register(parent)
cleanup.install_handlers()
if mode == "locked-registry":
    cleanup._LOCK.acquire()
ready_r, ready_w = os.pipe()
child_pid = os.fork()
if child_pid == 0:
    os.close(ready_r)
    if mode == "atexit":
        sys.exit(0)
    try:
        if mode in ("sigterm", "sigint"):
            os.write(ready_w, b"ready")
            while True:
                signal.pause()
        else:
            if mode in ("child-network", "locked-registry"):
                child = TrackedNetwork("child")
                cleanup.register(child)
            cleanup._run_cleanup()
    except KeyboardInterrupt:
        os._exit(128 + signal.SIGINT)
    except BaseException:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)

os.close(ready_w)
if mode == "locked-registry":
    cleanup._LOCK.release()
reaped = False
try:
    if mode in ("sigterm", "sigint"):
        assert select.select([ready_r], [], [], 3.0)[0], "child did not become ready"
        assert os.read(ready_r, 5) == b"ready"
        os.kill(child_pid, signal.SIGTERM if mode == "sigterm" else signal.SIGINT)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        pid, status = os.waitpid(child_pid, os.WNOHANG)
        if pid:
            reaped = True
            break
        time.sleep(0.01)
    assert reaped, "child cleanup blocked after fork"
    expected_exit = -signal.SIGTERM if mode == "sigterm" else (
        128 + signal.SIGINT if mode == "sigint" else 0
    )
    assert os.waitstatus_to_exitcode(status) == expected_exit
    child_events = [f"child:{child_pid}"] if mode in (
        "child-network", "locked-registry"
    ) else []
    actual = log.read_text().splitlines() if log.exists() else []
    assert actual == child_events, (actual, child_events)
    cleanup._run_cleanup()
    assert log.read_text().splitlines() == child_events + [f"parent:{os.getpid()}"]
finally:
    os.close(ready_r)
    if not reaped:
        os.kill(child_pid, signal.SIGKILL)
        os.waitpid(child_pid, 0)
    cleanup.unregister(parent)
"""


_ASYNC_FORK_SCRIPT = (
    "import asyncio\nasync def main():\n"
    "    import signal\n"
    "    signal.signal(signal.SIGINT, signal.default_int_handler)\n"
    + textwrap.indent(_FORK_SCRIPT, "    ")
    + "\nasyncio.run(main())\n"
)


@pytest.mark.parametrize("entry", ["sync", "async"])
@pytest.mark.parametrize(
    "mode", ["cleanup", "atexit", "sigterm", "sigint", "child-network", "locked-registry"]
)
def test_forked_child_cleans_up_only_its_own_networks(
    tmp_path: Path, mode: str, entry: str
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _FORK_SCRIPT if entry == "sync" else _ASYNC_FORK_SCRIPT,
            mode,
            str(tmp_path / "cleanup.log"),
        ],
        capture_output=True,
        text=True,
        timeout=12.0,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


_REGISTRATION_SCRIPT = """
import signal
import sys
import threading
from unittest.mock import patch

from p4net.network import _cleanup as cleanup

mode = sys.argv[1]
original_signal = signal.signal
previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

with patch.object(cleanup.atexit, "register", wraps=cleanup.atexit.register) as register:
    if mode == "worker-first":
        worker = threading.Thread(target=cleanup.install_handlers)
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert all(signal.getsignal(sig) == previous[sig] for sig in previous)
        cleanup.install_handlers()
    elif mode.startswith("retry-"):
        failed = getattr(signal, mode.removeprefix("retry-"))
        attempts = []
        def set_handler(sig, handler):
            attempts.append(sig)
            if sig == failed and attempts.count(sig) == 1:
                raise OSError("temporary signal registration failure")
            return original_signal(sig, handler)
        with patch.object(cleanup.signal, "signal", side_effect=set_handler):
            cleanup.install_handlers()
            cleanup.install_handlers()
        assert attempts.count(failed) == 2, attempts
        assert all(attempts.count(sig) == 1 for sig in previous if sig != failed)
    elif mode == "rearm":
        events = []
        def custom_handler(sig, frame):
            events.append("previous")
        signal.signal(signal.SIGTERM, custom_handler)
        previous[signal.SIGTERM] = custom_handler
        class TrackedNetwork:
            def stop(self):
                events.append("cleanup")
        net = TrackedNetwork()
        cleanup.register(net)
        try:
            cleanup.install_handlers()
            signal.raise_signal(signal.SIGTERM)
            assert events == ["cleanup", "previous"], events
            cleanup.install_handlers()
            signal.raise_signal(signal.SIGTERM)
            assert events == ["cleanup", "previous", "cleanup", "previous"], events
        finally:
            cleanup.unregister(net)
    else:
        with patch.object(cleanup.signal, "signal", wraps=original_signal) as install:
            cleanup.install_handlers()
            cleanup.install_handlers()
            cleanup.install_handlers()
        assert install.call_count == 2, install.call_count
    assert register.call_count == 1, register.call_count

if mode != "rearm":
    for sig, handler in previous.items():
        assert signal.getsignal(sig) != handler, (sig, cleanup._PREV_HANDLERS)
        assert cleanup._PREV_HANDLERS[sig] == handler, (sig, cleanup._PREV_HANDLERS)
"""


@pytest.mark.parametrize(
    "mode", ["worker-first", "retry-SIGINT", "retry-SIGTERM", "rearm", "repeat"]
)
def test_cleanup_hooks_register_independently_and_retry(mode: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _REGISTRATION_SCRIPT, mode],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


_ASYNC_SIGNAL_SCRIPT = """
import asyncio
import signal
import sys
import threading

from p4net.network import _cleanup as cleanup

sig = getattr(signal, sys.argv[1])
mode = sys.argv[2]
events = []

async def main():
    delivered = asyncio.Event()
    def previous(signum, frame):
        events.append("previous")
        delivered.set()
    signal.signal(sig, previous)

    class TrackedNetwork:
        def stop(self):
            events.append("sync-stop")
        async def astop(self):
            events.append("cleanup-start")
            if mode == "running":
                signal.raise_signal(sig)
            await asyncio.sleep(0.01)
            events.append("cleanup-finished")

    net = TrackedNetwork()
    cleanup.register(net)
    cleanup.install_handlers()
    try:
        if mode == "worker":
            def send():
                threading.Event().wait(0.05)
                signal.pthread_kill(threading.get_ident(), sig)
            loop = asyncio.get_running_loop()
            loop.call_later(0.01, lambda: threading.Thread(target=send).start())
        else:
            signal.raise_signal(sig)
        if mode == "queued":
            signal.raise_signal(sig)
        await asyncio.wait_for(delivered.wait(), timeout=3)
        assert events == ["cleanup-start", "cleanup-finished", "previous"], events
    finally:
        cleanup.unregister(net)

asyncio.run(main())
"""


@pytest.mark.parametrize("signum", ["SIGINT", "SIGTERM"])
@pytest.mark.parametrize("mode", ["once", "queued", "running", "worker"])
def test_signal_in_event_loop_waits_for_async_cleanup_before_redelivery(
    signum: str, mode: str
) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _ASYNC_SIGNAL_SCRIPT, signum, mode],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


_LOOP_REGISTRATION_SCRIPT = """
import asyncio
import signal
import sys
from unittest.mock import patch

from p4net.network import _cleanup as cleanup

mode = sys.argv[1]
previous = signal.getsignal(signal.SIGTERM)

async def install():
    loop = asyncio.get_running_loop()
    cleanup.install_handlers()
    assert cleanup._SIGNAL_LOOPS[signal.SIGTERM] is loop
    assert cleanup._PREV_HANDLERS[signal.SIGTERM] == previous

if mode == "upgrade":
    cleanup.install_handlers()
    asyncio.run(install())
elif mode == "retry":
    async def retry():
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler", side_effect=RuntimeError("unavailable")):
            cleanup.install_handlers()
        assert signal.SIGTERM not in cleanup._SIGNAL_LOOPS
        assert cleanup._PREV_HANDLERS[signal.SIGTERM] == previous
        assert callable(signal.getsignal(signal.SIGTERM))
        await install()
    asyncio.run(retry())
else:
    asyncio.run(install())
    asyncio.run(install())

cleanup.install_handlers()
assert signal.SIGTERM not in cleanup._SIGNAL_LOOPS
assert cleanup._PREV_HANDLERS[signal.SIGTERM] == previous
assert callable(signal.getsignal(signal.SIGTERM))
"""


@pytest.mark.parametrize("mode", ["upgrade", "retry", "closed"])
def test_signal_registration_tracks_event_loop_lifetime(mode: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _LOOP_REGISTRATION_SCRIPT, mode],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
