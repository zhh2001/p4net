"""End-to-end test for P4RuntimeClient.write_register and read_register.

Brings up a single-switch topology compiled from
``tests/fixtures/p4/register_demo.p4`` and exercises the new register API
against the real BMv2 + gRPC stack.

Run with:

    sudo -E env "PATH=$PATH" pytest \\
      --run-integration --run-p4c --run-bmv2 \\
      -m "integration and requires_p4c and requires_bmv2" \\
      tests/control/test_register_integration.py
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

import pytest

from p4net import Network
from p4net.control import EncodingError, NoSuchRegisterError
from p4net.topo import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_p4c,
    pytest.mark.requires_bmv2,
]

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "p4"
_REGISTER_DEMO = _FIXTURES / "register_demo.p4"


def _suffix() -> str:
    return uuid.uuid4().hex[:6]


def _two_free_ports() -> tuple[int, int]:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as a,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as b,
    ):
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        return int(a.getsockname()[1]), int(b.getsockname()[1])


def _bring_up(tmp_path: Path) -> tuple[Network, str]:
    suffix = _suffix()
    s1 = f"s{suffix}"
    grpc, thrift = _two_free_ports()
    topo = Topology()
    topo.add_host(f"h{suffix}", ip="10.0.0.1/24")
    topo.add_switch(s1, p4_src=_REGISTER_DEMO, grpc_port=grpc, thrift_port=thrift)
    topo.add_link(f"h{suffix}", s1, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    return net, s1


def test_write_then_read_individual_cells(tmp_path: Path) -> None:
    """Write 3 cells, read each back individually."""
    net, s1 = _bring_up(tmp_path)
    try:
        sw = net.switch(s1)
        writes = {0: 0xCAFEBABE, 3: 0xDEADBEEF, 7: 42}
        for idx, value in writes.items():
            sw.client.write_register("MyIngress.test_register", index=idx, value=value)
        for idx, expected in writes.items():
            got = sw.client.read_register("MyIngress.test_register", index=idx)
            assert got == expected, f"cell {idx}: expected {expected:#x}, got {got!r}"
    finally:
        net.stop()


def test_write_then_read_full_array(tmp_path: Path) -> None:
    """Write 3 cells, read the whole array back; unwritten cells are zero."""
    net, s1 = _bring_up(tmp_path)
    try:
        sw = net.switch(s1)
        writes = {0: 100, 5: 500, 15: 1500}
        for idx, value in writes.items():
            sw.client.write_register("MyIngress.test_register", index=idx, value=value)
        out = sw.client.read_register("MyIngress.test_register")
        assert isinstance(out, list)
        assert len(out) == 16
        for idx, expected in writes.items():
            assert out[idx] == expected, f"cell {idx}: expected {expected}, got {out[idx]}"
        # Cells we didn't touch are zero (BMv2 initializes registers to zero).
        for idx in range(16):
            if idx not in writes:
                assert out[idx] == 0, f"cell {idx} should be zero, got {out[idx]}"
        # Stash for the report.
        (tmp_path / "register_readback.txt").write_text(repr(out))
    finally:
        net.stop()


def test_write_value_exceeding_bitwidth_raises_before_grpc(tmp_path: Path) -> None:
    """Encoding-side validation rejects oversized values before any gRPC call."""
    net, s1 = _bring_up(tmp_path)
    try:
        sw = net.switch(s1)
        with pytest.raises(EncodingError, match="does not fit in 32 bits"):
            sw.client.write_register("MyIngress.test_register", index=0, value=2**32)
    finally:
        net.stop()


def test_read_nonexistent_register_raises(tmp_path: Path) -> None:
    net, s1 = _bring_up(tmp_path)
    try:
        sw = net.switch(s1)
        with pytest.raises(NoSuchRegisterError, match="no register named 'never_defined'"):
            sw.client.read_register("never_defined")
    finally:
        net.stop()
