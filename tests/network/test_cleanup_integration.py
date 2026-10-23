"""Signal cleanup in fresh processes with real Linux namespaces."""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_SCRIPT = """
import asyncio
import sys

from p4net import Network
from p4net.network import _cleanup as cleanup
from p4net.runtime import NetworkNamespace
from p4net.topo import Topology

name, mode = sys.argv[1:]

async def main():
    topo = Topology()
    topo.add_host(name, ip="10.247.0.1/24",
                  ip6="fd00:38::1/64" if mode == "starting-ipv6" else None)
    if mode == "starting-ipv6":
        topo.add_host(name + "a", ip6="fd00:38::2/64")
        topo.add_link(name, name + "a")
    net = Network(topo)
    if mode.startswith("starting-"):
        def pause():
            print("ready", flush=True)
            assert sys.stdin.readline() == "release\\n"

        if mode == "starting-ipv6":
            execute = NetworkNamespace.exec
            paused = False
            def execute_paused(self, argv, **kwargs):
                nonlocal paused
                if argv == ["ip", "-j", "-6", "addr", "show"] and not paused:
                    paused = True
                    pause()
                return execute(self, argv, **kwargs)
            NetworkNamespace.exec = execute_paused
        elif mode == "starting-namespace":
            create = NetworkNamespace.create
            def create_paused(self):
                create(self)
                pause()
            NetworkNamespace.create = create_paused
        else:
            validate = topo.validate
            def validate_paused():
                validate()
                pause()
            topo.validate = validate_paused

        finish_stop = net._finish_stop
        def finish_stop_announced(future):
            print("stopping", flush=True)
            finish_stop(future)
        net._finish_stop = finish_stop_announced
        await net.astart()
        raise AssertionError("startup completed despite shutdown")

    if mode == "astart":
        await net.astart()
    else:
        if mode == "main-first":
            cleanup.install_handlers()
        await asyncio.to_thread(net.start)
        if mode == "worker-first":
            cleanup.install_handlers()
    print("ready", flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
"""


@pytest.mark.parametrize(
    "mode",
    [
        "worker-first",
        "main-first",
        "astart",
        "starting-validate",
        "starting-namespace",
        "starting-ipv6",
    ],
)
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_waits_for_namespace_cleanup(mode: str, signum: signal.Signals) -> None:
    name = "h" + uuid.uuid4().hex[:6]
    namespace = Path("/var/run/netns") / name
    assert not namespace.exists()
    proc = subprocess.Popen(
        [sys.executable, "-c", _SCRIPT, name, mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        assert select.select([proc.stdout], [], [], 15)[0], "network did not become ready"
        line = proc.stdout.readline()
        if line != "ready\n":
            output, errors = proc.communicate(timeout=5)
            pytest.fail(line + output + errors)
        assert namespace.exists() == (mode != "starting-validate")
        proc.send_signal(signum)
        if mode.startswith("starting-"):
            assert select.select([proc.stdout], [], [], 5)[0], "shutdown did not start"
            assert proc.stdout.readline() == "stopping\n"
            assert namespace.exists() == (mode != "starting-validate")
            assert proc.stdin is not None
            proc.stdin.write("release\n")
            proc.stdin.flush()
        output, errors = proc.communicate(timeout=15)
        assert proc.returncode == -signum, (proc.returncode, output, errors)
        assert not namespace.exists(), f"namespace {name} survived {signum.name}"
        if mode == "starting-ipv6":
            assert not (namespace.parent / (name + "a")).exists()
    finally:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=5)
        if namespace.exists():
            subprocess.run(["ip", "netns", "delete", name], check=True, capture_output=True)
