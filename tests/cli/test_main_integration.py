"""Load a Python topology through the CLI and verify real forwarding."""

from __future__ import annotations

import contextlib
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from p4net.cli import main as cli_main

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_p4c,
    pytest.mark.requires_bmv2,
]

_PORT_SWAP = Path(__file__).resolve().parents[2] / "examples/quick_start/quick_start.p4"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def cli_topology(tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    suffix = uuid.uuid4().hex[:6]
    h1, h2, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    grpc_port = _free_port()
    thrift_port = _free_port()
    while thrift_port == grpc_port:
        thrift_port = _free_port()
    marker = tmp_path / "connected"
    topology = tmp_path / "topology.py"
    topology.write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "from pathlib import Path\n"
        "import pickle\n"
        "from typing import ClassVar, get_type_hints\n"
        "from p4net.topo import Topology\n"
        "@dataclass\n"
        "class HostConfig:\n"
        "    prefix: ClassVar[str] = '10.249.0.'\n"
        "    number: int\n"
        "    @property\n"
        "    def ip(self):\n"
        "        return f'{self.prefix}{self.number}/24'\n"
        "topology = Topology()\n"
        f"topology.add_host({h1!r}, ip=HostConfig(1).ip)\n"
        f"topology.add_host({h2!r}, ip=HostConfig(2).ip)\n"
        f"topology.add_switch({switch!r}, p4_src=Path({str(_PORT_SWAP)!r}),\n"
        f"                    grpc_port={grpc_port}, thrift_port={thrift_port})\n"
        f"topology.add_link({h1!r}, {switch!r}, port_b=1)\n"
        f"topology.add_link({h2!r}, {switch!r}, port_b=2)\n"
        "def setup(net):\n"
        "    assert pickle.loads(pickle.dumps(HostConfig(1))) == HostConfig(1)\n"
        "    assert get_type_hints(HostConfig)['number'] is int\n"
        f"    assert net.pingall() == {{({h1!r}, {h2!r}): True, ({h2!r}, {h1!r}): True}}\n"
        f"    Path({str(marker)!r}).write_text('connected')\n"
    )
    return topology, marker, h1, h2, switch


def test_cli_dataclass_topology_starts_and_runs_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_topology: tuple[Path, Path, str, str, str]
) -> None:
    topology, marker, h1, h2, _ = cli_topology
    monkeypatch.setattr(cli_main, "_wait_for_signal", lambda: None)
    previous = set(sys.modules)

    try:
        assert (
            cli_main.main([str(topology), "--no-shell", "--log-dir", str(tmp_path / "logs")]) == 0
        )
        assert marker.read_text() == "connected"
        assert not (Path("/var/run/netns") / h1).exists()
        assert not (Path("/var/run/netns") / h2).exists()
    finally:
        for name in set(sys.modules) - previous:
            if name.startswith("_p4net_topology_"):
                sys.modules.pop(name, None)


@pytest.mark.parametrize(
    ("verb", "shutdown"),
    [("listen", "exit"), ("listen", "hangup"), ("ping", "exit"), ("ping6", "exit")],
)
def test_ctrl_c_cancels_command_without_stopping_topology(
    tmp_path: Path, cli_topology: tuple[Path, Path, str, str, str], verb: str, shutdown: str
) -> None:
    topology, _, h1, h2, switch = cli_topology
    namespaces = [Path("/var/run/netns") / name for name in (h1, h2)]
    script = """
import fcntl
import signal
import sys
import termios
from pathlib import Path

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.signal(signal.SIGHUP, signal.SIG_DFL)
from p4net.cli import shell
from p4net.cli.main import main

shell._DEFAULT_HISTORY = Path(sys.argv.pop(1))
raise SystemExit(main())
"""
    master, slave = os.openpty()
    pending = b""

    def read_through(marker: bytes, timeout: float = 10) -> bytes:
        nonlocal pending
        deadline = time.monotonic() + timeout
        while marker not in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                pytest.fail(f"Timed out waiting for {marker!r}. Terminal output: {pending!r}")
            try:
                chunk = os.read(master, 4096)
            except OSError as exc:
                pytest.fail(f"Terminal closed waiting for {marker!r}. Output: {pending!r} ({exc})")
            if not chunk:
                pytest.fail(f"Terminal closed waiting for {marker!r}. Output: {pending!r}")
            pending += chunk
        output, _, pending = pending.partition(marker)
        return output

    try:
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path / "history"),
                str(topology),
                "--log-dir",
                str(tmp_path / "logs"),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env={**os.environ, "TERM": "xterm", "PROMPT_TOOLKIT_NO_CPR": "1"},
        ) as proc:
            os.close(slave)
            slave = -1
            try:
                read_through(b"p4net>", timeout=30)
                assert all(path.exists() for path in namespaces)
                if verb == "listen":
                    os.write(master, f"{switch} packet listen timeout=30\n".encode())
                    read_through(f"Waiting for 1 packet on {switch}".encode())
                else:
                    target = "::1" if verb == "ping6" else h2
                    os.write(master, f"{h1} {verb} {target} 30\n".encode())
                    read_through(b"seq=1  time")
                os.write(master, b"\x03")
                output = read_through(b"^C cancelled")
                if verb != "listen":
                    stats = re.search(rb"(\d+) sent, (\d+) received, [\d.]+% loss", output)
                    assert stats is not None, output
                    sent, received = map(int, stats.groups())
                    assert 1 <= received <= sent < 30
                read_through(b"p4net>")

                os.write(master, b"status\n")
                read_through(b"running:   ")
                assert read_through(b"\r\n").rstrip(b"\r") == b"True"
                assert all(path.exists() for path in namespaces)
                read_through(b"p4net>")
                os.write(master, b"pingall\n")
                read_through(b"2/2 succeeded")
                read_through(b"p4net>")

                if shutdown == "exit":
                    os.write(master, b"exit\n")
                    read_through(b"Stopping topology...")
                    assert proc.wait(timeout=10) == 0
                else:
                    os.close(master)
                    master = -1
                    assert proc.wait(timeout=10) == -signal.SIGHUP
                assert not any(path.exists() for path in namespaces)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=5)
    finally:
        if master != -1:
            os.close(master)
        if slave != -1:
            os.close(slave)
