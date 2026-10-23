"""RANGE encoding and validation with real BMv2 table operations."""

from __future__ import annotations

import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from p4net import Network
from p4net.cli import CommandDispatcher
from p4net.compiler import P4Compiler
from p4net.control import AsyncP4RuntimeClient, EncodingError
from p4net.topo import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_p4c,
    pytest.mark.requires_bmv2,
]

_SOURCE = Path(__file__).resolve().parents[1] / "fixtures/p4/match_types.p4"
_TABLE = "MyIngress.l2_match"
_ACTION = "MyIngress.set_egress_port"


@pytest.fixture
def network(tmp_path: Path) -> Iterator[Network]:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.246.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(second, ip="10.246.0.2/24", mac="00:00:00:00:00:02")
    with socket.socket() as grpc, socket.socket() as thrift:
        grpc.bind(("127.0.0.1", 0))
        thrift.bind(("127.0.0.1", 0))
        topo.add_switch(
            switch, _SOURCE, grpc_port=grpc.getsockname()[1], thrift_port=thrift.getsockname()[1]
        )
    topo.add_link(first, switch, port_b=1)
    topo.add_link(second, switch, port_b=2)
    with Network(
        topo, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as net:
        for sender, target, mac in (
            (first, "10.246.0.2", "00:00:00:00:00:02"),
            (second, "10.246.0.1", "00:00:00:00:00:01"),
        ):
            host = net.host(sender)
            host.exec(
                [
                    "ip",
                    "neigh",
                    "replace",
                    target,
                    "lladdr",
                    mac,
                    "dev",
                    next(iter(host.interfaces)),
                    "nud",
                    "permanent",
                ]
            )
        yield net


@pytest.mark.parametrize("client_kind", ["sync", "async"])
async def test_full_range_crud_and_forwarding(network: Network, client_kind: str) -> None:
    switch = next(iter(network.switches.values()))
    first, second = network.hosts
    sync = switch.client
    client = (
        AsyncP4RuntimeClient(
            grpc_address=("127.0.0.1", switch.descriptor.grpc_port),
            device_id=switch.descriptor.device_id,
            info_index=sync.index,
            election_id=(sync.election_id[0] + 1, 0),
        )
        if client_kind == "async"
        else sync
    )
    if client_kind == "async":
        await client.connect()

    async def write(operation: str, *args: Any, **kwargs: Any) -> None:
        with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
            result = getattr(client, f"{operation}_table_entry")(
                _TABLE, *args, priority=17, **kwargs
            )
            if client_kind == "async":
                await result
            rpc.assert_called_once()
            entry = rpc.call_args.args[0].updates[0].entity.table_entry
            assert entry.priority == 17
            (field,) = entry.match
            assert field.HasField("exact")

    async def read() -> list[dict[str, Any]]:
        if client_kind == "async":
            return [entry async for entry in client.list_table_entries(_TABLE)]
        return client.list_table_entries(_TABLE)

    def match(mac: str) -> dict[str, object]:
        return {
            "hdr.ethernet.dstAddr": mac,
            "hdr.ethernet.srcAddr": (0, 0),
            "hdr.ethernet.etherType": (0, 65535),
        }

    forward, reverse = match("00:00:00:00:00:02"), match("00:00:00:00:00:01")
    try:
        await write("insert", forward, _ACTION, {"port": 2})
        await write("insert", reverse, _ACTION, {"port": 1})
        entries = await read()
        assert len(entries) == 2
        assert all(set(entry["match"]) == {"hdr.ethernet.dstAddr"} for entry in entries)
        assert all(entry["priority"] == 17 for entry in entries)
        assert all(network.pingall().values())

        await write("modify", forward, "MyIngress.drop")
        assert not network.ping(first, second, count=1, timeout=0.1)
        await write("modify", forward, _ACTION, {"port": 2})
        assert all(network.pingall().values())
        await write("delete", forward)
        (remaining,) = await read()
        assert set(remaining["match"]) == {"hdr.ethernet.dstAddr"}
        assert int.from_bytes(remaining["match"]["hdr.ethernet.dstAddr"], "big") == 1
        await write("delete", reverse)
        assert await read() == []
    finally:
        if client_kind == "async":
            await client.disconnect()


@pytest.mark.parametrize("client_kind", ["sync", "async"])
async def test_reversed_range_never_writes(network: Network, client_kind: str) -> None:
    switch = next(iter(network.switches.values()))
    sync = switch.client
    client = (
        AsyncP4RuntimeClient(
            grpc_address=("127.0.0.1", switch.descriptor.grpc_port),
            device_id=switch.descriptor.device_id,
            info_index=sync.index,
            election_id=(sync.election_id[0] + 1, 0),
        )
        if client_kind == "async"
        else sync
    )
    if client_kind == "async":
        await client.connect()

    async def write(operation: str, *args: Any, **kwargs: Any) -> None:
        result = getattr(client, f"{operation}_table_entry")(_TABLE, *args, **kwargs)
        if client_kind == "async":
            await result

    async def read() -> list[dict[str, Any]]:
        if client_kind == "async":
            return [entry async for entry in client.list_table_entries(_TABLE)]
        return client.list_table_entries(_TABLE)

    forward = {
        "hdr.ethernet.dstAddr": "00:00:00:00:00:02",
        "hdr.ethernet.srcAddr": (0, 0),
        "hdr.ethernet.etherType": (0, 65535),
    }
    reverse = dict(forward, **{"hdr.ethernet.dstAddr": "00:00:00:00:00:01"})
    invalid = dict(forward, **{"hdr.ethernet.etherType": (0x900, 0x800)})
    singleton = dict(forward, **{"hdr.ethernet.etherType": (0x800, 0x800)})
    try:
        await write("insert", forward, _ACTION, {"port": 2}, priority=17)
        await write("insert", reverse, _ACTION, {"port": 1}, priority=17)
        assert all(network.pingall().values())

        def entry_key(entry: dict[str, Any]) -> bytes:
            return entry["match"]["hdr.ethernet.dstAddr"]

        before = sorted(await read(), key=entry_key)
        for operation in ("insert", "modify", "delete"):
            args = (invalid,) if operation == "delete" else (invalid, _ACTION, {"port": 2})
            with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
                with pytest.raises(EncodingError, match=r"lower bound.*upper bound"):
                    await write(operation, *args, priority=17)
                rpc.assert_not_called()
        if client_kind == "sync":
            cli = CommandDispatcher(network)
            for operation in ("add", "del"):
                command = (
                    f"{switch.name} table {operation} {_TABLE} match: "
                    "hdr.ethernet.dstAddr=00:00:00:00:00:02,"
                    "hdr.ethernet.srcAddr=0&&&0,hdr.ethernet.etherType=0x900..0x800 "
                )
                if operation == "add":
                    command += f"action: {_ACTION} params: port=2 "
                with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
                    output = cli.dispatch(command + "priority: 17")
                    assert output.startswith("error: EncodingError:") and "upper bound" in output
                    rpc.assert_not_called()
        assert sorted(await read(), key=entry_key) == before
        assert all(network.pingall().values())

        await write("insert", singleton, _ACTION, {"port": 2}, priority=18)
        (entry,) = [entry for entry in await read() if entry["priority"] == 18]
        assert entry["match"]["hdr.ethernet.etherType"] == (b"\x08\x00", b"\x08\x00")
        assert all(network.pingall().values())
        await write("delete", singleton, priority=18)
        await write("delete", forward, priority=17)
        await write("delete", reverse, priority=17)
        assert await read() == []
    finally:
        if client_kind == "async":
            await client.disconnect()
