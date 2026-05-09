"""Integration tests: real `simple_switch_grpc` driven by `P4RuntimeClient`.

Gated by both `requires_bmv2` and `requires_p4c`. None require root. The
shared `compiled_artifacts` fixture compiles `simple_routing.p4` once per
test session and hands out paths to its bmv2 JSON and P4Info.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from p4net.compiler import P4Compiler
from p4net.control import (
    CounterData,
    DuplicateEntryError,
    EntryNotFoundError,
    NotPrimaryError,
    P4InfoIndex,
    P4RuntimeClient,
)
from p4net.runtime import BMv2Switch

pytestmark = [pytest.mark.requires_bmv2, pytest.mark.requires_p4c]

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "p4"
_SIMPLE_ROUTING = _FIXTURES / "simple_routing.p4"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def compiled_artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    cache = tmp_path_factory.mktemp("p4rt-compiler-cache")
    compiler = P4Compiler(cache_dir=cache)
    result = compiler.compile(_SIMPLE_ROUTING)
    return {"bmv2_json": result.bmv2_json, "p4info": result.p4info}


@pytest.fixture
def bmv2(compiled_artifacts: dict[str, Path], tmp_path: Path) -> Iterator[BMv2Switch]:
    grpc_port = _free_port()
    thrift_port = _free_port()
    sw = BMv2Switch(
        "s_p4rt",
        device_id=0,
        grpc_port=grpc_port,
        thrift_port=thrift_port,
        bmv2_json=compiled_artifacts["bmv2_json"],
        port_to_iface={},
        log_dir=tmp_path / "logs",
        pcap_dir=tmp_path / "pcaps",
        startup_timeout=10.0,
    )
    sw.start()
    try:
        sw.wait_until_ready()
        yield sw
    finally:
        sw.stop()


@pytest.fixture
def client(bmv2: BMv2Switch, compiled_artifacts: dict[str, Path]) -> Iterator[P4RuntimeClient]:
    c = P4RuntimeClient(bmv2.grpc_address, device_id=0, election_id=(10, 0))
    c.connect(timeout=5.0)
    try:
        c.set_pipeline_config(
            bmv2_json=compiled_artifacts["bmv2_json"],
            p4info=compiled_artifacts["p4info"],
        )
        yield c
    finally:
        c.disconnect()


# ---------------------------------------------------------------------------
# 1. Connect + set pipeline + get pipeline
# ---------------------------------------------------------------------------


def test_connect_and_set_get_pipeline(
    bmv2: BMv2Switch, compiled_artifacts: dict[str, Path]
) -> None:
    c = P4RuntimeClient(bmv2.grpc_address, device_id=0, election_id=(10, 0))
    c.connect(timeout=5.0)
    try:
        c.set_pipeline_config(
            bmv2_json=compiled_artifacts["bmv2_json"],
            p4info=compiled_artifacts["p4info"],
        )
        bmv2_data, idx = c.get_pipeline_config()
        assert isinstance(idx, P4InfoIndex)
        assert "MyIngress.ipv4_lpm" in idx.table_names
        assert len(bmv2_data) > 0
    finally:
        c.disconnect()


# ---------------------------------------------------------------------------
# 2. Insert + list + delete an LPM entry
# ---------------------------------------------------------------------------


def test_insert_list_delete_lpm(client: P4RuntimeClient) -> None:
    client.insert_table_entry(
        "MyIngress.ipv4_lpm",
        {"hdr.ipv4.dstAddr": "10.0.1.0/24"},
        "MyIngress.set_egress_port",
        {"port": 2},
    )
    entries = client.list_table_entries("MyIngress.ipv4_lpm")
    assert len(entries) == 1
    e = entries[0]
    assert e["table"] == "MyIngress.ipv4_lpm"
    assert e["action"] == "MyIngress.set_egress_port"
    assert e["params"] == {"port": b"\x02"}
    # match value is canonical bytes; "10.0.1.0" has no leading 0x00 bytes
    assert e["match"]["hdr.ipv4.dstAddr"] == (b"\x0a\x00\x01\x00", 24)

    client.delete_table_entry("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.1.0/24"})
    assert client.list_table_entries("MyIngress.ipv4_lpm") == []


# ---------------------------------------------------------------------------
# 3. Duplicate insert
# ---------------------------------------------------------------------------


def test_duplicate_insert_raises(client: P4RuntimeClient) -> None:
    client.insert_table_entry(
        "MyIngress.ipv4_lpm",
        {"hdr.ipv4.dstAddr": "10.0.2.0/24"},
        "MyIngress.set_egress_port",
        {"port": 3},
    )
    with pytest.raises(DuplicateEntryError):
        client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.2.0/24"},
            "MyIngress.set_egress_port",
            {"port": 3},
        )


# ---------------------------------------------------------------------------
# 4. Delete non-existent
# ---------------------------------------------------------------------------


def test_delete_nonexistent_raises(client: P4RuntimeClient) -> None:
    with pytest.raises(EntryNotFoundError):
        client.delete_table_entry("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.99.99.0/24"})


# ---------------------------------------------------------------------------
# 5. Read counter
# ---------------------------------------------------------------------------


def test_read_counter_default_is_zero(client: P4RuntimeClient) -> None:
    client.insert_table_entry(
        "MyIngress.ipv4_lpm",
        {"hdr.ipv4.dstAddr": "10.0.3.0/24"},
        "MyIngress.set_egress_port",
        {"port": 4},
    )
    # We have not actually injected packets, so the counter is unmodified.
    result = client.read_counter("MyIngress.ingress_pkts", 0)
    assert isinstance(result, CounterData)
    assert result.packet_count == 0


# ---------------------------------------------------------------------------
# 6. Multicast group lifecycle
# ---------------------------------------------------------------------------


def test_multicast_group_add_list_delete(client: P4RuntimeClient) -> None:
    client.add_multicast_group(1, [1, 2, 3])
    groups = client.list_multicast_groups()
    assert groups == {1: [1, 2, 3]}
    client.delete_multicast_group(1)
    assert client.list_multicast_groups() == {}


# ---------------------------------------------------------------------------
# 7. NotPrimaryError
# ---------------------------------------------------------------------------


def test_not_primary_error_for_lower_election_id(
    bmv2: BMv2Switch, compiled_artifacts: dict[str, Path]
) -> None:
    a = P4RuntimeClient(bmv2.grpc_address, device_id=0, election_id=(10, 0))
    a.connect(timeout=5.0)
    try:
        # Push a pipeline so the device is fully active.
        a.set_pipeline_config(
            bmv2_json=compiled_artifacts["bmv2_json"],
            p4info=compiled_artifacts["p4info"],
        )
        b = P4RuntimeClient(bmv2.grpc_address, device_id=0, election_id=(1, 0))
        with pytest.raises(NotPrimaryError):
            b.connect(timeout=5.0)
        b.disconnect()
    finally:
        a.disconnect()
    # Once A is gone, B can become primary on a fresh client.
    # Give BMv2 a moment to notice the disconnect.
    time.sleep(0.5)
    b2 = P4RuntimeClient(bmv2.grpc_address, device_id=0, election_id=(1, 0))
    b2.connect(timeout=5.0)
    try:
        assert b2.is_connected()
    finally:
        b2.disconnect()


# ---------------------------------------------------------------------------
# 8. Pipeline push idempotency
# ---------------------------------------------------------------------------


def test_pipeline_push_idempotent(bmv2: BMv2Switch, compiled_artifacts: dict[str, Path]) -> None:
    c = P4RuntimeClient(bmv2.grpc_address, device_id=0, election_id=(10, 0))
    c.connect(timeout=5.0)
    try:
        c.set_pipeline_config(
            bmv2_json=compiled_artifacts["bmv2_json"],
            p4info=compiled_artifacts["p4info"],
        )
        # Second push with VERIFY_AND_COMMIT must succeed.
        c.set_pipeline_config(
            bmv2_json=compiled_artifacts["bmv2_json"],
            p4info=compiled_artifacts["p4info"],
        )
    finally:
        c.disconnect()
