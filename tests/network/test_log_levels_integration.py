"""Validate imported log levels and preserve them in real BMv2 processes."""

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
_SOURCE = Path(__file__).resolve().parents[1] / "fixtures/p4/simple_routing.p4"


def _topology() -> tuple[Topology, str, str, str]:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, mac="00:00:00:00:00:01")
    topo.add_host(second, mac="00:00:00:00:00:02")
    with socket.socket() as grpc, socket.socket() as thrift:
        grpc.bind(("127.0.0.1", 0))
        thrift.bind(("127.0.0.1", 0))
        topo.add_switch(
            switch,
            _SOURCE,
            cpu_port=1,
            grpc_port=grpc.getsockname()[1],
            thrift_port=thrift.getsockname()[1],
        )
    for number, host, port in ((1, first, 0), (2, second, 2)):
        topo.add_link(host, switch, port_a=0, port_b=port, ip_a=f"10.235.0.{number}/24")
    return topo, first, second, switch


@pytest.mark.parametrize("level", [[], ["info"], {}, {"level": "info"}])
def test_invalid_imported_log_level_rejected_before_compilation(
    tmp_path: Path, level: object
) -> None:
    topo, _first, _second, switch = _topology()
    data = topo.to_dict()
    data["switches"][switch]["log_level"] = level
    original = deepcopy(data)
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    logs = tmp_path / "logs"
    with patch.object(compiler, "compile", wraps=compiler.compile) as compile_call:
        with pytest.raises(TopologyError, match="invalid log_level"):
            restored = Topology.from_dict(data)
            with Network(restored, compiler=compiler, log_dir=logs):
                pass
        compile_call.assert_not_called()
    assert not logs.exists()
    assert data == original


@pytest.mark.requires_p4c
@pytest.mark.requires_bmv2
@pytest.mark.parametrize("level", ["trace", "debug", "info", "warn", "error", None])
def test_imported_log_level_preserved_in_process_and_forwarding(
    tmp_path: Path, level: str | None
) -> None:
    topo, first, second, switch = _topology()
    data = topo.to_dict()
    if level is None:
        del data["switches"][switch]["log_level"]
    else:
        data["switches"][switch]["log_level"] = level
    original = deepcopy(data)
    restored = Topology.from_dict(data)
    before = restored.to_dict()
    expected = "info" if level is None else level
    with Network(
        restored, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as network:
        running = network.switch(switch)
        assert running.descriptor.log_level == expected
        assert running.bmv2.pid is not None
        argv = Path(f"/proc/{running.bmv2.pid}/cmdline").read_bytes().split(b"\0")
        assert argv[argv.index(b"-L") + 1].decode() == expected
        for number, name in ((1, first), (2, second)):
            host = network.host(name)
            assert host.primary_ip == f"10.235.0.{number}"
            host.exec(
                [
                    "ip",
                    "neigh",
                    "replace",
                    f"10.235.0.{3 - number}",
                    "lladdr",
                    f"00:00:00:00:00:0{3 - number}",
                    "dev",
                    next(iter(host.interfaces)),
                    "nud",
                    "permanent",
                ]
            )
        routes = (("10.235.0.1/32", 0), ("10.235.0.2/32", 2))
        for address, port in routes:
            running.client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": address},
                "MyIngress.set_egress_port",
                {"port": port},
            )
        outcomes = network.pingall()
        assert len(outcomes) == 2 and all(outcomes.values())
        for address, _port in routes:
            running.client.delete_table_entry("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": address})
        assert running.client.list_table_entries("MyIngress.ipv4_lpm") == []
        assert restored.to_dict() == before and data == original
