"""Readiness checks against real TCP listeners and child processes."""

from __future__ import annotations

import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from p4net.runtime import BMv2StartupError, BMv2Switch

_SERVER = """\
import socket
import sys
import time
from pathlib import Path

grpc = sys.argv[sys.argv.index('--grpc-server-addr') + 1]
thrift = int(sys.argv[sys.argv.index('--thrift-port') + 1])
mode = Path(sys.argv[sys.argv.index('--') - 1])
listeners = []
for service, host, port in [('grpc', *grpc.rsplit(':', 1)), ('thrift', '127.0.0.1', thrift)]:
    if mode.name not in ('both', 'shared', service):
        continue
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if mode.name == 'shared':
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((host, int(port)))
    sock.listen()
    listeners.append(sock)
Path(str(mode) + '.ready').touch()
time.sleep(30)
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def python_switch(tmp_path: Path) -> Iterator[Callable[..., BMv2Switch]]:
    binary = tmp_path / "tcp-server"
    binary.write_text(f"#!{sys.executable}\n{_SERVER}")
    binary.chmod(0o755)
    switches: list[BMv2Switch] = []

    def make_switch(
        *,
        mode: str = "both",
        grpc_port: int | None = None,
        thrift_port: int | None = None,
        grpc_bind_addr: str = "127.0.0.1",
    ) -> BMv2Switch:
        sw = BMv2Switch(
            f"s{len(switches)}",
            device_id=0,
            grpc_port=_free_port() if grpc_port is None else grpc_port,
            thrift_port=_free_port() if thrift_port is None else thrift_port,
            bmv2_json=tmp_path / mode,
            port_to_iface={},
            log_dir=tmp_path,
            grpc_bind_addr=grpc_bind_addr,
            binary=str(binary),
        )
        switches.append(sw)
        sw.start()
        return sw

    yield make_switch
    for sw in switches:
        sw.stop()


@pytest.mark.parametrize("service", ["gRPC", "Thrift"])
@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_readiness_rejects_foreign_listener(
    python_switch: Callable[..., BMv2Switch], service: str, host: str
) -> None:
    with socket.create_server((host, 0)) as holder:
        port = int(holder.getsockname()[1])
        sw = python_switch(mode="none", **{f"{service.lower()}_port": port})
        with pytest.raises(BMv2StartupError, match=f"{service}.*another process"):
            sw.wait_until_ready(timeout=0.5)
        assert holder.getsockname()[1] == port


@pytest.mark.parametrize("mode", ["grpc", "thrift"])
def test_readiness_requires_both_owned_listeners(
    python_switch: Callable[..., BMv2Switch], mode: str
) -> None:
    sw = python_switch(mode=mode)
    with pytest.raises(BMv2StartupError, match="ports within"):
        sw.wait_until_ready(timeout=0.5)
    assert sw.is_running()


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "localhost"])
def test_readiness_accepts_owned_listeners(
    python_switch: Callable[..., BMv2Switch], host: str, tmp_path: Path
) -> None:
    sw = python_switch(grpc_bind_addr=host)
    sw.wait_until_ready(timeout=3.0)
    assert sw.is_running()
    assert (tmp_path / "both.ready").exists()


def test_readiness_rejects_shared_grpc_listener(
    python_switch: Callable[..., BMv2Switch], tmp_path: Path
) -> None:
    with socket.socket() as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = int(holder.getsockname()[1])
        sw = python_switch(mode="shared", grpc_port=port)
        deadline = time.monotonic() + 3.0
        while not (tmp_path / "shared.ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (tmp_path / "shared.ready").exists(), sw.log_file.read_text()
        with pytest.raises(BMv2StartupError, match=r"gRPC.*another process"):
            sw.wait_until_ready(timeout=0.5)


def test_readiness_ignores_other_ipv4_address(
    python_switch: Callable[..., BMv2Switch],
) -> None:
    with socket.create_server(("127.0.0.2", 0)) as holder:
        sw = python_switch(grpc_port=int(holder.getsockname()[1]))
        sw.wait_until_ready(timeout=3.0)


def test_readiness_ignores_ipv6_only_listener(
    python_switch: Callable[..., BMv2Switch],
) -> None:
    with socket.socket(socket.AF_INET6) as holder:
        holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        holder.bind(("::", 0))
        holder.listen()
        sw = python_switch(grpc_port=int(holder.getsockname()[1]))
        sw.wait_until_ready(timeout=3.0)


def test_readiness_rejects_foreign_dual_stack_listener(
    python_switch: Callable[..., BMv2Switch],
) -> None:
    with socket.socket(socket.AF_INET6) as holder:
        holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        holder.bind(("::", 0))
        holder.listen()
        sw = python_switch(mode="none", grpc_port=int(holder.getsockname()[1]))
        with pytest.raises(BMv2StartupError, match=r"gRPC.*another process"):
            sw.wait_until_ready(timeout=0.5)
