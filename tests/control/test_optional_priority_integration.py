"""OPTIONAL entries, wildcard priority and forwarding on a real BMv2 switch."""

from __future__ import annotations

import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from p4net import Network
from p4net.cli import CommandDispatcher
from p4net.compiler import P4Compiler
from p4net.control import AsyncP4RuntimeClient
from p4net.topo import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_p4c,
    pytest.mark.requires_bmv2,
]

_SOURCE = Path(__file__).resolve().parents[1] / "fixtures/p4/optional_match.p4"
_TABLE = "MyIngress.optional_acl"


@pytest.fixture
def network(tmp_path: Path) -> Iterator[Network]:
    suffix = uuid.uuid4().hex[:6]
    h1, h2, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(h1, ip="10.248.0.1/24")
    topo.add_host(h2, ip="10.248.0.2/24")
    with socket.socket() as grpc, socket.socket() as thrift:
        grpc.bind(("127.0.0.1", 0))
        thrift.bind(("127.0.0.1", 0))
        topo.add_switch(
            switch,
            _SOURCE,
            grpc_port=grpc.getsockname()[1],
            thrift_port=thrift.getsockname()[1],
        )
    topo.add_link(h1, switch, port_b=1)
    topo.add_link(h2, switch, port_b=2)
    with Network(
        topo, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as net:
        yield net


@pytest.mark.parametrize("client_kind", ["sync", "async"])
async def test_optional_entries_preserve_priority_and_forward(
    network: Network, client_kind: str
) -> None:
    switch = next(iter(network.switches.values()))
    first, second = network.hosts
    sync = switch.client
    async_client = AsyncP4RuntimeClient(
        grpc_address=("127.0.0.1", switch.descriptor.grpc_port),
        device_id=switch.descriptor.device_id,
        info_index=sync.index,
        election_id=(sync.election_id[0] + 1, 0),
    )
    if client_kind == "async":
        await async_client.connect()

    async def write(
        operation: str, match: dict[str, object], action: str | None, priority: int
    ) -> None:
        client = async_client if client_kind == "async" else sync
        method = getattr(client, f"{operation}_table_entry")
        args = (_TABLE, match) if action is None else (_TABLE, match, action)
        result = method(*args, priority=priority)
        if client_kind == "async":
            await result

    async def read() -> list[dict[str, Any]]:
        if client_kind == "async":
            return [entry async for entry in async_client.list_table_entries(_TABLE)]
        return sync.list_table_entries(_TABLE)

    match = {"hdr.ethernet.etherType": 0x0800}
    try:
        await write("insert", {}, "MyIngress.forward", 1)
        await write("insert", match, "MyIngress.drop", 20)
        entries = sorted(await read(), key=lambda entry: entry["priority"])
        assert [entry["priority"] for entry in entries] == [1, 20]
        assert [entry["match"] for entry in entries] == [
            {},
            {"hdr.ethernet.etherType": b"\x08\x00"},
        ]
        assert not network.ping(first, second, count=1, timeout=0.1)

        await write("modify", match, "MyIngress.forward", 20)
        assert all(network.pingall().values())
        await write("delete", match, None, 20)
        (remaining,) = await read()
        assert remaining["match"] == {} and remaining["priority"] == 1
        await write("delete", {}, None, 1)
        assert await read() == []

        if client_kind == "sync":
            cli = CommandDispatcher(network)
            assert (
                cli.dispatch(
                    f"{switch.name} table add {_TABLE} match: hdr.ethernet.etherType=0x0800 "
                    "action: MyIngress.forward priority: 30"
                )
                == "ok"
            )
            output = cli.dispatch(f"{switch.name} table dump {_TABLE}")
            assert "priority: 30" in output and "2048" in output
            assert (
                cli.dispatch(
                    f"{switch.name} table del {_TABLE} "
                    "match: hdr.ethernet.etherType=0x0800 priority: 30"
                )
                == "ok"
            )
            assert await read() == []
    finally:
        if client_kind == "async":
            await async_client.disconnect()
