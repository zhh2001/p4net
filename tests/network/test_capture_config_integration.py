"""Validate imported capture settings and check real BMv2 packet captures."""

from __future__ import annotations

import socket
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from p4net import Network
from p4net.compiler import P4Compiler
from p4net.topo import Topology, TopologyError

pytestmark = pytest.mark.integration
_SOURCE = Path(__file__).resolve().parents[1] / "fixtures/p4/two_port_swap.p4"


def _topology() -> tuple[Topology, str, str, str]:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.236.0.1/24")
    topo.add_host(second, ip="10.236.0.2/24")
    with socket.socket() as grpc, socket.socket() as thrift:
        grpc.bind(("127.0.0.1", 0))
        thrift.bind(("127.0.0.1", 0))
        topo.add_switch(
            switch,
            _SOURCE,
            grpc_port=grpc.getsockname()[1],
            thrift_port=thrift.getsockname()[1],
        )
    topo.add_link(first, switch, port_b=1)
    topo.add_link(second, switch, port_b=2)
    return topo, first, second, switch


@pytest.mark.parametrize("value", [None, 0, 1, 0.0, 1.0, "false", "true", "", b"false", [], {}])
def test_invalid_imported_capture_flag_rejected_before_compilation(
    tmp_path: Path, value: object
) -> None:
    topo, _first, _second, switch = _topology()
    data = topo.to_dict()
    data["switches"][switch]["pcap_enabled"] = value
    original = deepcopy(data)
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    logs, captures = tmp_path / "logs", tmp_path / "pcaps"
    with patch.object(compiler, "compile", wraps=compiler.compile) as compile_call:
        with pytest.raises(TopologyError, match=r"pcap_enabled.*bool"):
            restored = Topology.from_dict(data)
            with Network(restored, compiler=compiler, log_dir=logs, pcap_dir=captures):
                pass
        compile_call.assert_not_called()
    assert not logs.exists() and not captures.exists()
    assert data == original


@pytest.mark.requires_p4c
@pytest.mark.requires_bmv2
@pytest.mark.parametrize("value", [True, False, "default"])
@pytest.mark.parametrize("capture_enabled", [False, True])
def test_imported_capture_flag_controls_real_packet_captures(
    tmp_path: Path, value: bool | str, capture_enabled: bool
) -> None:
    topo, first, second, switch = _topology()
    data = topo.to_dict()
    if value == "default":
        del data["switches"][switch]["pcap_enabled"]
    else:
        data["switches"][switch]["pcap_enabled"] = value
    original = deepcopy(data)
    restored = Topology.from_dict(data)
    before = restored.to_dict()
    expected_flag = value is not False
    captures = tmp_path / "pcaps"
    marker = b"p4net-pcap-flag"
    with Network(
        restored,
        compiler=P4Compiler(cache_dir=tmp_path / "compiler"),
        log_dir=tmp_path / "logs",
        pcap_dir=captures if capture_enabled else None,
    ) as network:
        running = network.switch(switch)
        assert running.descriptor.pcap_enabled is expected_flag
        assert running.bmv2.pid is not None
        argv = Path(f"/proc/{running.bmv2.pid}/cmdline").read_bytes().split(b"\0")
        assert (b"--pcap" in argv) is (capture_enabled and expected_flag)
        if capture_enabled and expected_flag:
            assert argv[argv.index(b"--pcap") + 1].decode() == str(captures)
        network.host(first).exec(
            ["ping", "-n", "-c", "1", "-W", "2", "-p", marker.hex(), "10.236.0.2"],
            capture_output=True,
            timeout=5,
        )
        assert network.ping(second, first) is True
        assert restored.to_dict() == before and data == original
    paths = list(captures.glob("*.pcap"))
    expected = (
        {f"{switch}-eth{port}_{direction}.pcap" for port in (1, 2) for direction in ("in", "out")}
        if capture_enabled and expected_flag
        else set()
    )
    assert {path.name for path in paths} == expected
    for path in paths:
        assert marker in path.read_bytes(), path
