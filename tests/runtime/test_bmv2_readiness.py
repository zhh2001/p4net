"""Readiness checks against real TCP listeners and child processes."""

from __future__ import annotations

import errno
import json
import socket
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

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


def _reserve_port(host: str, excluded: set[int]) -> socket.socket:
    rejected: list[socket.socket] = []
    try:
        while True:
            sock = socket.socket()
            rejected.append(sock)
            sock.bind((host, 0))
            if int(sock.getsockname()[1]) not in excluded:
                rejected.pop()
                return sock
            # Keep excluded ports bound so the next attempt selects another one.
    finally:
        for sock in rejected:
            sock.close()


@contextmanager
def _other_address_listener(host: str, family: int = socket.AF_INET) -> Iterator[socket.socket]:
    with ExitStack() as cleanup:
        while True:
            reserved = cleanup.enter_context(_reserve_port("127.0.0.1", set()))
            port = int(reserved.getsockname()[1])
            holder = cleanup.enter_context(socket.socket(family))
            if family == socket.AF_INET6:
                holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                holder.bind((host, port))
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    continue
                raise
            holder.listen()
            reserved.close()
            yield holder
            return


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
        with ExitStack() as reservations:
            excluded = {port for port in (grpc_port, thrift_port) if port is not None}
            if grpc_port is None:
                holder = reservations.enter_context(_reserve_port(grpc_bind_addr, excluded))
                grpc_port = int(holder.getsockname()[1])
                excluded.add(grpc_port)
            if thrift_port is None:
                holder = reservations.enter_context(_reserve_port("127.0.0.1", excluded))
                thrift_port = int(holder.getsockname()[1])
            sw = BMv2Switch(
                f"s{len(switches)}",
                device_id=0,
                grpc_port=grpc_port,
                thrift_port=thrift_port,
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


@pytest.mark.parametrize("mode", ["none", "grpc", "thrift"])
def test_timeout_preserves_live_child_diagnostics_before_cleanup(
    python_switch: Callable[..., BMv2Switch], tmp_path: Path, mode: str
) -> None:
    sw = python_switch(mode=mode)
    ready = tmp_path / f"{mode}.ready"
    deadline = time.monotonic() + 3.0
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), sw.log_file.read_text()

    with pytest.raises(BMv2StartupError, match="ports within") as caught:
        sw.wait_until_ready(timeout=0.5)
    (path,) = tmp_path.glob(f"{sw.name}.startup-*.json")
    assert str(path) in str(caught.value)
    record = json.loads(path.read_text())
    assert record["pid"] == sw.pid
    assert record["returncode"] is None
    assert record["timeout_seconds"] == 0.5
    assert record["completed_polls"] > 0
    sample = record["last_observation"]
    assert len(sample["grpc_socket_inodes"]) == int(mode == "grpc")
    assert len(sample["thrift_socket_inodes"]) == int(mode == "thrift")
    assert set(sample["grpc_socket_inodes"] + sample["thrift_socket_inodes"]) <= set(
        sample["owned_socket_inodes"]
    )
    assert str(sw.pid) in record["process"]["stat"]
    assert str(sw.pid) in record["threads"]
    assert record["process"]["status"]
    assert record["process"]["wchan"]
    assert sw.is_running()
    sw.stop()
    assert json.loads(path.read_text()) == record


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
    with _other_address_listener("127.0.0.2") as holder:
        sw = python_switch(grpc_port=int(holder.getsockname()[1]))
        sw.wait_until_ready(timeout=3.0)


def test_readiness_ignores_ipv6_only_listener(
    python_switch: Callable[..., BMv2Switch],
) -> None:
    with _other_address_listener("::", socket.AF_INET6) as holder:
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


def test_auto_service_ports_are_reserved_and_distinct(
    python_switch: Callable[..., BMv2Switch],
) -> None:
    real_socket = socket.socket
    first_port: int | None = None
    first_still_reserved: list[bool] = []

    class PreferFirstPort(real_socket):
        def bind(self, address: Any) -> None:
            nonlocal first_port
            if self.family != socket.AF_INET or address != ("127.0.0.1", 0):
                super().bind(address)
                return
            if first_port is None:
                super().bind(address)
                first_port = int(self.getsockname()[1])
                return
            try:
                super().bind(("127.0.0.1", first_port))
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                first_still_reserved.append(True)
                super().bind(address)
            else:
                first_still_reserved.append(False)

    with patch.object(socket, "socket", PreferFirstPort):
        sw = python_switch()
        assert sw.grpc_port != sw.thrift_port
        sw.wait_until_ready(timeout=3.0)
    assert first_still_reserved and all(first_still_reserved)


@pytest.mark.parametrize("field", ["grpc_port", "thrift_port"])
def test_auto_service_port_excludes_explicit_port(
    python_switch: Callable[..., BMv2Switch], field: str
) -> None:
    real_socket = socket.socket
    with real_socket() as selected:
        selected.bind(("127.0.0.1", 0))
        explicit_port = int(selected.getsockname()[1])
    attempted = False

    class PreferExplicitPort(real_socket):
        def bind(self, address: Any) -> None:
            nonlocal attempted
            if self.family != socket.AF_INET or address != ("127.0.0.1", 0):
                super().bind(address)
                return
            attempted = True
            try:
                super().bind(("127.0.0.1", explicit_port))
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                super().bind(address)

    with patch.object(socket, "socket", PreferExplicitPort):
        sw = python_switch(**{field: explicit_port})
        assert getattr(sw, field) == explicit_port
        assert sw.grpc_port != sw.thrift_port
        sw.wait_until_ready(timeout=3.0)
    assert attempted


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_ignored_address_port_is_selected_on_grpc_bind_address(
    python_switch: Callable[..., BMv2Switch], family: int
) -> None:
    real_socket = socket.socket
    with real_socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        occupied_port = int(occupied.getsockname()[1])
        other_host = "127.0.0.2" if family == socket.AF_INET else "::"

        class PreferOccupiedPort(real_socket):
            def bind(self, address: Any) -> None:
                if self.family == family and address == (other_host, 0):
                    super().bind((other_host, occupied_port))
                else:
                    super().bind(address)

        selected: list[BMv2Switch] = []

        def make_switch(**kwargs: Any) -> BMv2Switch:
            sw = python_switch(**kwargs)
            selected.append(sw)
            return sw

        with patch.object(socket, "socket", PreferOccupiedPort):
            if family == socket.AF_INET:
                test_readiness_ignores_other_ipv4_address(make_switch)
            else:
                test_readiness_ignores_ipv6_only_listener(make_switch)
        assert len(selected) == 1
        assert selected[0].grpc_port != occupied_port
        assert occupied.getsockname()[1] == occupied_port
