"""LPM host-bit validation and zero-prefix wildcards on real IPv4/IPv6 networks."""

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

_PROJECT = Path(__file__).resolve().parents[2]
_ACTION = "MyIngress.set_egress_port"
_CONFIG = {
    4: {
        "source": _PROJECT / "tests/fixtures/p4/simple_routing.p4",
        "table": "MyIngress.ipv4_lpm",
        "field": "hdr.ipv4.dstAddr",
        "addresses": ("10.241.1.1", "10.241.2.2"),
        "host_prefix": 16,
        "prefix": 24,
        "networks": ("10.241.1.0/24", "10.241.2.0/24"),
        "wildcard": "10.99.88.77/0",
        "width": 32,
    },
    6: {
        "source": _PROJECT / "examples/ipv6_lpm/ipv6_lpm.p4",
        "table": "MyIngress.ipv6_lpm",
        "field": "hdr.ipv6.dstAddr",
        "addresses": ("fd00:48:1::1", "fd00:48:2::2"),
        "host_prefix": 32,
        "prefix": 64,
        "networks": ("fd00:48:1::/64", "fd00:48:2::/64"),
        "wildcard": "2001:db8:ab::dead/0",
        "width": 128,
    },
}


@pytest.fixture
def network(tmp_path: Path, family: int) -> Iterator[Network]:
    config = _CONFIG[family]
    suffix = uuid.uuid4().hex[:6]
    names = (f"h{suffix}a", f"h{suffix}b")
    switch = f"s{suffix}"
    macs = ("00:00:00:00:00:01", "00:00:00:00:00:02")
    topo = Topology()
    for name, address, mac in zip(names, config["addresses"], macs, strict=True):
        kwargs = {"ip6" if family == 6 else "ip": f"{address}/{config['host_prefix']}"}
        topo.add_host(name, mac=mac, **kwargs)
    with socket.socket() as grpc, socket.socket() as thrift:
        grpc.bind(("127.0.0.1", 0))
        thrift.bind(("127.0.0.1", 0))
        topo.add_switch(
            switch,
            config["source"],
            grpc_port=grpc.getsockname()[1],
            thrift_port=thrift.getsockname()[1],
        )
    for port, name in enumerate(names, 1):
        topo.add_link(name, switch, port_b=port)
    with Network(
        topo, compiler=P4Compiler(cache_dir=tmp_path / "compiler"), log_dir=tmp_path / "logs"
    ) as net:
        for index, name in enumerate(names):
            host = net.host(name)
            host.exec(
                [
                    "ip",
                    "-6" if family == 6 else "-4",
                    "neigh",
                    "replace",
                    config["addresses"][1 - index],
                    "lladdr",
                    macs[1 - index],
                    "dev",
                    next(iter(host.interfaces)),
                    "nud",
                    "permanent",
                ]
            )
        yield net


@pytest.mark.parametrize("family", [4, 6], ids=["ipv4", "ipv6"])
@pytest.mark.parametrize("client_kind", ["sync", "async"])
async def test_lpm_host_bits_and_zero_prefix(
    network: Network, client_kind: str, family: int
) -> None:
    config = _CONFIG[family]
    table, field = config["table"], config["field"]
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

    async def write(operation: str, *args: Any) -> None:
        result = getattr(client, f"{operation}_table_entry")(table, *args)
        if client_kind == "async":
            await result

    async def read() -> list[dict[str, Any]]:
        entries = (
            [entry async for entry in client.list_table_entries(table)]
            if client_kind == "async"
            else client.list_table_entries(table)
        )
        return sorted(entries, key=lambda entry: repr(entry["match"]))

    def connected() -> bool:
        outcomes = network.pingall6() if family == 6 else network.pingall()
        return len(outcomes) == 2 and all(outcomes.values())

    forward = {field: config["networks"][1]}
    reverse = {field: config["networks"][0]}
    bad_address = config["addresses"][1]
    bad_string = f"{bad_address}/{config['prefix']}"
    wildcard = {field: config["wildcard"]}
    full = {field: f"{bad_address}/{config['width']}"}
    try:
        await write("insert", forward, _ACTION, {"port": 2})
        await write("insert", reverse, _ACTION, {"port": 1})
        assert connected()
        before = await read()
        for value in (bad_string, (bad_address, config["prefix"])):
            for operation in ("insert", "modify", "delete"):
                args = (
                    ({field: value},)
                    if operation == "delete"
                    else ({field: value}, _ACTION, {"port": 2})
                )
                with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
                    with pytest.raises(EncodingError, match="outside the prefix"):
                        await write(operation, *args)
                    rpc.assert_not_called()
        if client_kind == "sync":
            cli = CommandDispatcher(network)
            for verb in ("add", "del"):
                command = f"{switch.name} table {verb} {table} match: {field}={bad_string}"
                if verb == "add":
                    command += f" action: {_ACTION} params: port=2"
                with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
                    output = cli.dispatch(command)
                    assert (
                        output.startswith("error: EncodingError:")
                        and "outside the prefix" in output
                    )
                    rpc.assert_not_called()
        assert await read() == before
        assert connected()

        await write("insert", full, _ACTION, {"port": 2})
        assert len(await read()) == 3 and connected()
        await write("delete", full)
        with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
            await write("insert", wildcard, "MyIngress.drop")
            rpc.assert_called_once()
            assert len(rpc.call_args.args[0].updates[0].entity.table_entry.match) == 0
        assert len([entry for entry in await read() if not entry["match"]]) == 1
        assert connected()
        await write("delete", wildcard)
        if client_kind == "sync":
            cli = CommandDispatcher(network)
            for verb in ("add", "del"):
                command = f"{switch.name} table {verb} {table} match: {field}={config['wildcard']}"
                if verb == "add":
                    command += " action: MyIngress.drop"
                with patch.object(client._stub, "Write", wraps=client._stub.Write) as rpc:
                    assert cli.dispatch(command) == "ok"
                    rpc.assert_called_once()
                    assert len(rpc.call_args.args[0].updates[0].entity.table_entry.match) == 0
        await write("delete", forward)
        await write("delete", reverse)
        assert await read() == []
    finally:
        if client_kind == "async":
            await client.disconnect()
