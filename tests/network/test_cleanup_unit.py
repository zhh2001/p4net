"""Process isolation for network cleanup hooks."""

from __future__ import annotations

import subprocess
import sys
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


@pytest.mark.parametrize(
    "mode", ["cleanup", "atexit", "sigterm", "sigint", "child-network", "locked-registry"]
)
def test_forked_child_cleans_up_only_its_own_networks(tmp_path: Path, mode: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _FORK_SCRIPT, mode, str(tmp_path / "cleanup.log")],
        capture_output=True,
        text=True,
        timeout=12.0,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
