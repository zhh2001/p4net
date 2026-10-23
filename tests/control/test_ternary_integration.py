"""TERNARY masked-bit validation, wildcards and forwarding on a real BMv2 switch."""

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
_FIELD = "hdr.ethernet.srcAddr"


@pytest.fixture
def network(tmp_path: Path) -> Iterator[Network]:
    suffix = uuid.uuid4().hex[:6]
    first, second, switch = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    topo = Topology()
    topo.add_host(first, ip="10.245.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(second, ip="10.245.0.2/24", mac="00:00:00:00:00:02")
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
            (first, "10.245.0.2", "00:00:00:00:00:02"),
            (second, "10.245.0.1", "00:00:00:00:00:01"),
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
async def test_ternary_masked_bits_and_zero_mask(network: Network, client_kind: str) -> None:
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

    async def write(operation: str, *args: Any, priority: int = 17) -> None:
        result = getattr(client, f"{operation}_table_entry")(_TABLE, *args, priority=priority)
        if client_kind == "async":
            await result

    async def read() -> list[dict[str, Any]]:
        entries = (
            [entry async for entry in client.list_table_entries(_TABLE)]
            if client_kind == "async"
            else client.list_table_entries(_TABLE)
        )
        return sorted(entries, key=lambda entry: repr(entry["match"]))

    def connected() -> bool:
        outcomes = network.pingall()
        return len(outcomes) == 2 and all(outcomes.values())

    def match(destination: int, source: object) -> dict[str, object]:
        return {
            "hdr.ethernet.dstAddr": destination,
            _FIELD: source,
            "hdr.ethernet.etherType": (0, 65535),
        }

    forward, reverse = match(2, (1, (1 << 48) - 1)), match(1, (2, (1 << 48) - 1))
    try:
        await write("insert", forward, _ACTION, {"port": 2})
        await write("insert", reverse, _ACTION, {"port": 1})
        assert connected()
        before = await read()
        for value in (
            ("00:00:00:00:00:01", "ff:ff:ff:ff:ff:fe"),
            (1, (1 << 48) - 2),
            (b"\x01", b"\xff" * 5 + b"\xfe"),
        ):
            for operation in ("insert", "modify", "delete"):
                key = match(2, value)
                args = (key,) if operation == "delete" else (key, _ACTION, {"port": 2})
                with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
                    with pytest.raises(EncodingError, match="outside the mask"):
                        await write(operation, *args)
                    rpc.assert_not_called()
        if client_kind == "sync":
            cli = CommandDispatcher(network)
            for verb in ("add", "del"):
                command = (
                    f"{switch.name} table {verb} {_TABLE} match: hdr.ethernet.dstAddr=2,"
                    f"{_FIELD}=00:00:00:00:00:01&&&ff:ff:ff:ff:ff:fe priority: 17"
                )
                if verb == "add":
                    command += f" action: {_ACTION} params: port=2"
                with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
                    output = cli.dispatch(command)
                    assert (
                        output.startswith("error: EncodingError:") and "outside the mask" in output
                    )
                    rpc.assert_not_called()
        assert await read() == before
        assert connected()

        sparse = match(2, (b"\x01", b"\x15"))
        await write("insert", sparse, "MyIngress.drop", priority=18)
        (entry,) = [entry for entry in await read() if entry["priority"] == 18]
        assert entry["match"][_FIELD] == (b"\x01", b"\x15")
        assert not network.ping(first, second, count=1, timeout=0.1)
        await write("modify", sparse, _ACTION, {"port": 2}, priority=18)
        assert connected()
        await write("delete", sparse, priority=18)

        wildcard = match(2, ("11:22:33:44:55:66", 0))
        with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
            await write("insert", wildcard, "MyIngress.drop", priority=19)
            rpc.assert_called_once()
            (field,) = rpc.call_args.args[0].updates[0].entity.table_entry.match
            assert field.HasField("exact")
        (entry,) = [entry for entry in await read() if entry["priority"] == 19]
        assert entry["match"] == {"hdr.ethernet.dstAddr": b"\x02"}
        assert not network.ping(first, second, count=1, timeout=0.1)
        await write("modify", match(2, (b"\x01", b"")), _ACTION, {"port": 2}, priority=19)
        assert connected()
        await write("delete", wildcard, priority=19)
        if client_kind == "sync":
            cli = CommandDispatcher(network)
            for verb in ("add", "del"):
                command = (
                    f"{switch.name} table {verb} {_TABLE} match: hdr.ethernet.dstAddr=2,"
                    f"{_FIELD}=0xabcdef&&&0 priority: 19"
                )
                if verb == "add":
                    command += " action: MyIngress.drop"
                with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
                    assert cli.dispatch(command) == "ok"
                    rpc.assert_called_once()
                    (field,) = rpc.call_args.args[0].updates[0].entity.table_entry.match
                    assert field.HasField("exact")
                if verb == "add":
                    assert not network.ping(first, second, count=1, timeout=0.1)
            assert connected()
        await write("delete", forward)
        await write("delete", reverse)
        assert await read() == []
    finally:
        if client_kind == "async":
            await client.disconnect()
