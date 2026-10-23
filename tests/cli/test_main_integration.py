"""Load a Python topology through the CLI and verify real forwarding."""

from __future__ import annotations

import socket
import sys
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


def test_cli_dataclass_topology_starts_and_runs_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
