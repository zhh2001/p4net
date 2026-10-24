"""Reject invalid loss inputs and read back real directional netem settings."""

from __future__ import annotations

import json
import socket
import subprocess
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from p4net import Network
from p4net.compiler import P4Compiler
from p4net.runtime import TcError, apply_netem
from p4net.topo import Topology, TopologyError

pytestmark = pytest.mark.integration
_SOURCE = Path(__file__).resolve().parents[1] / "fixtures/p4/two_port_swap.p4"
_FIELDS = (
    "loss_pct",
    "loss_pct_a_to_b",
    "loss_pct_b_to_a",
    "loss_pct_a_to_b_extra",
    "loss_pct_b_to_a_extra",
)


def _topology(layout: str) -> tuple[Topology, str, str]:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.238.0.1/24")
    topo.add_host(second, ip="10.238.0.2/24")
    if layout == "direct":
        topo.add_link(first, second)
    else:
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
        topo.add_link(switch, second, port_a=2)
    return topo, first, second


def _values(field: str, value: object) -> dict[str, Any]:
    values = {field: value}
    if field.endswith("_extra"):
        values["loss_pct"] = 1
    return values


def _read_loss(network: Network, endpoint: dict[str, Any]) -> float | None:
    command = ["tc", "-j", "qdisc", "show", "dev", endpoint["iface_name"]]
    if endpoint["node"] in network.topology.hosts:
        output = network.host(endpoint["node"]).exec(command, capture_output=True).stdout
    else:
        output = subprocess.check_output(command)
    netem = [entry for entry in json.loads(output) if entry["kind"] == "netem"]
    if not netem:
        return None
    assert len(netem) == 1
    return float(netem[0]["options"].get("loss-random", {}).get("loss", 0)) * 100


@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), "1", True])
def test_invalid_imported_loss_rejected_before_compilation(
    tmp_path: Path, field: str, value: object
) -> None:
    topo, _first, _second = _topology("switch")
    data = topo.to_dict()
    data["links"][0].update(_values(field, value))
    original = deepcopy(data)
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    logs = tmp_path / "logs"
    with patch.object(compiler, "compile", wraps=compiler.compile) as compile_call:
        with pytest.raises(TopologyError, match=rf"{field}.*finite"):
            restored = Topology.from_dict(data)
            with Network(restored, compiler=compiler, log_dir=logs):
                pass
        compile_call.assert_not_called()
    assert not logs.exists()
    assert data == original


@pytest.mark.parametrize(
    "layout",
    ["direct", pytest.param("switch", marks=[pytest.mark.requires_p4c, pytest.mark.requires_bmv2])],
)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in _FIELDS
        for value in (0, 0.5, 99 if field.endswith("_extra") else 100)
    ],
)
def test_imported_loss_configures_kernel_directions(
    tmp_path: Path, layout: str, field: str, value: float
) -> None:
    topo, first, second = _topology(layout)
    data = topo.to_dict()
    data["links"][0].update(_values(field, value))
    original = deepcopy(data)
    restored = Topology.from_dict(data)
    before = restored.to_dict()
    expected: dict[str, float | None] = {"a": None, "b": None}
    if field == "loss_pct":
        expected = {"a": value, "b": value}
    elif field.endswith("_extra"):
        expected = {"a": 1, "b": 1}
        expected["a" if "a_to_b" in field else "b"] = 1 + value
    else:
        expected["a" if "a_to_b" in field else "b"] = value
    with Network(
        restored, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as network:
        for side, percentage in expected.items():
            actual = _read_loss(network, data["links"][0][side])
            if percentage is None:
                assert actual is None
            else:
                assert actual == pytest.approx(percentage, abs=0.00001)
        if all(percentage in (None, 0) for percentage in expected.values()):
            assert network.ping(first, second) is True
            assert network.ping(second, first) is True
        assert restored.to_dict() == before and data == original


@pytest.mark.requires_p4c
@pytest.mark.requires_bmv2
@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("value", [True, "1", float("nan"), float("inf")])
def test_invalid_runtime_loss_preserves_kernel_qdisc(tmp_path: Path, side: str, value: Any) -> None:
    topo, _first, _second = _topology("switch")
    data = topo.to_dict()
    data["links"][0]["loss_pct"] = 0.5
    restored = Topology.from_dict(data)
    with Network(
        restored, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as network:
        endpoint = data["links"][0][side]
        namespace = (
            network.host(endpoint["node"]).namespace if endpoint["node"] in restored.hosts else None
        )
        before = _read_loss(network, endpoint)
        import p4net.runtime.tc as tc

        with patch.object(tc, "_run", wraps=tc._run) as run:
            with pytest.raises(TcError, match="loss_pct"):
                apply_netem(namespace, endpoint["iface_name"], loss_pct=value)
            run.assert_not_called()
        assert _read_loss(network, endpoint) == before
