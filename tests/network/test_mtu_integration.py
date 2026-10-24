"""Validate imported MTUs and read back both ends of real network links."""

from __future__ import annotations

import json
import socket
import subprocess
import uuid
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from p4net import Network
from p4net.compiler import P4Compiler
from p4net.topo import Topology, TopologyError

pytestmark = pytest.mark.integration
_SOURCE = Path(__file__).resolve().parents[1] / "fixtures/p4/two_port_swap.p4"


def _topology(layout: str) -> tuple[Topology, str, str]:
    suffix = uuid.uuid4().hex[:6]
    first, second = f"h{suffix}a", f"h{suffix}b"
    switches = [] if layout == "direct" else [f"s{suffix}a"]
    if layout == "chain":
        switches.append(f"s{suffix}b")
    topo = Topology()
    topo.add_host(first, ip="10.237.0.1/24")
    topo.add_host(second, ip="10.237.0.2/24")
    with ExitStack() as stack:
        for switch in switches:
            grpc = stack.enter_context(socket.socket())
            thrift = stack.enter_context(socket.socket())
            grpc.bind(("127.0.0.1", 0))
            thrift.bind(("127.0.0.1", 0))
            topo.add_switch(
                switch,
                _SOURCE,
                grpc_port=grpc.getsockname()[1],
                thrift_port=thrift.getsockname()[1],
            )
    if not switches:
        topo.add_link(first, second)
    else:
        topo.add_link(first, switches[0], port_b=1)
        if len(switches) == 2:
            topo.add_link(switches[0], switches[1], port_a=2, port_b=1)
        topo.add_link(switches[-1], second, port_a=2)
    return topo, first, second


@pytest.mark.parametrize(
    "mtu",
    [
        True,
        False,
        1500.0,
        1500.5,
        "1500",
        b"1500",
        float("nan"),
        float("inf"),
        -float("inf"),
        [],
        {},
    ],
)
def test_invalid_imported_mtu_rejected_before_compilation(tmp_path: Path, mtu: object) -> None:
    topo, _first, _second = _topology("switch")
    data = topo.to_dict()
    data["links"][0]["mtu"] = mtu
    original = deepcopy(data)
    compiler = P4Compiler(cache_dir=tmp_path / "compiler")
    logs = tmp_path / "logs"
    with patch.object(compiler, "compile", wraps=compiler.compile) as compile_call:
        with pytest.raises(TopologyError, match=r"mtu.*int"):
            restored = Topology.from_dict(data)
            with Network(restored, compiler=compiler, log_dir=logs):
                pass
        compile_call.assert_not_called()
    assert not logs.exists()
    assert data == original


@pytest.mark.parametrize(
    "layout",
    [
        "direct",
        pytest.param("switch", marks=[pytest.mark.requires_p4c, pytest.mark.requires_bmv2]),
        pytest.param("chain", marks=[pytest.mark.requires_p4c, pytest.mark.requires_bmv2]),
    ],
)
@pytest.mark.parametrize("mtu", [None, 68, 1500, 65535, "default"])
def test_imported_mtu_configures_both_link_ends_and_forwards(
    tmp_path: Path, layout: str, mtu: int | str | None
) -> None:
    topo, first, second = _topology(layout)
    data = topo.to_dict()
    for link in data["links"]:
        if mtu == "default":
            del link["mtu"]
        else:
            link["mtu"] = mtu
    original = deepcopy(data)
    restored = Topology.from_dict(data)
    before = restored.to_dict()
    expected = 1500 if mtu is None or mtu == "default" else mtu
    with Network(
        restored, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as network:
        root_links = {
            info["ifname"]: info
            for info in json.loads(subprocess.check_output(["ip", "-j", "link", "show"], text=True))
        }
        for link in data["links"]:
            for endpoint in (link["a"], link["b"]):
                name, interface = endpoint["node"], endpoint["iface_name"]
                if name in restored.hosts:
                    (info,) = json.loads(
                        network.host(name)
                        .exec(["ip", "-j", "link", "show", "dev", interface], capture_output=True)
                        .stdout
                    )
                else:
                    info = root_links[interface]
                assert info["mtu"] == expected, (name, interface, info)
        for name, address in ((first, "10.237.0.2"), (second, "10.237.0.1")):
            result = network.host(name).exec(
                ["ping", "-n", "-c", "1", "-W", "2", "-s", "8", address],
                capture_output=True,
                timeout=5,
            )
            assert result.returncode == 0
        assert restored.to_dict() == before and data == original
