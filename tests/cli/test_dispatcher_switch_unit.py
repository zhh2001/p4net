"""Unit tests for `CommandDispatcher` switch verbs (table/counter/mcast/log)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from p4.config.v1 import p4info_pb2

import p4net.control  # ensures protobuf python-impl env var is set  # noqa: F401
from p4net.cli import CommandDispatcher
from p4net.cli.exceptions import CLIUsageError
from p4net.control import P4InfoIndex
from p4net.network import Network


def _build_p4info() -> p4info_pb2.P4Info:
    p = p4info_pb2.P4Info()
    a_no = p.actions.add()
    a_no.preamble.id = 1001
    a_no.preamble.name = "NoAction"
    a_set = p.actions.add()
    a_set.preamble.id = 1002
    a_set.preamble.name = "MyIngress.set_egress_port"
    pa = a_set.params.add()
    pa.id = 1
    pa.name = "port"
    pa.bitwidth = 9
    t_lpm = p.tables.add()
    t_lpm.preamble.id = 2001
    t_lpm.preamble.name = "MyIngress.ipv4_lpm"
    mf = t_lpm.match_fields.add()
    mf.id = 1
    mf.name = "hdr.ipv4.dstAddr"
    mf.bitwidth = 32
    mf.match_type = p4info_pb2.MatchField.LPM
    t_lpm.action_refs.add().id = 1001
    t_lpm.action_refs.add().id = 1002
    return p


def _make_switch(grpc: str = "127.0.0.1:50051") -> MagicMock:
    sw = MagicMock(name="RunningSwitch-s1")
    bmv2 = MagicMock()
    bmv2.grpc_address = grpc
    bmv2.pid = 12345
    bmv2.log_file = Path("/tmp/s1.log")
    sw.bmv2 = bmv2
    sw.client = MagicMock()
    sw.client.index = P4InfoIndex(_build_p4info())
    return sw


@pytest.fixture
def network() -> MagicMock:
    n = MagicMock(spec=Network)
    n.hosts = {}
    n.switches = {"s1": _make_switch()}
    n.is_running = True
    n.log_dir = Path("/tmp/p4net")
    n.host = lambda name: n.hosts[name]
    n.switch = lambda name: n.switches[name]
    return n


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


def test_switch_log(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    out = d.dispatch("s1 log")
    assert out == "/tmp/s1.log"


def test_switch_log_no_args(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    with pytest.raises(CLIUsageError, match="takes no arguments"):
        d.dispatch("s1 log extra")


# ---------------------------------------------------------------------------
# table list / dump
# ---------------------------------------------------------------------------


def test_switch_table_list(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    out = d.dispatch("s1 table list")
    assert "MyIngress.ipv4_lpm" in out
    assert "lpm" in out
    assert "hdr.ipv4.dstAddr" in out


def test_switch_table_dump_empty(network: MagicMock) -> None:
    network.switches["s1"].client.list_table_entries = MagicMock(return_value=[])
    d = CommandDispatcher(network)
    out = d.dispatch("s1 table dump MyIngress.ipv4_lpm")
    assert "empty" in out


def test_switch_table_dump_renders_entry(network: MagicMock) -> None:
    network.switches["s1"].client.list_table_entries = MagicMock(
        return_value=[
            {
                "table": "MyIngress.ipv4_lpm",
                "match": {"hdr.ipv4.dstAddr": (b"\x0a\x00\x00\x00", 24)},
                "action": "MyIngress.set_egress_port",
                "params": {"port": b"\x02"},
                "priority": None,
            }
        ]
    )
    d = CommandDispatcher(network)
    out = d.dispatch("s1 table dump MyIngress.ipv4_lpm")
    assert "MyIngress.ipv4_lpm" in out
    assert "MyIngress.set_egress_port" in out
    # Match value rendered as human IPv4/CIDR, not raw bytes.
    assert "10.0.0.0/24" in out
    assert "b'\\n" not in out
    # Action params decoded as decimals (port is a 9-bit field).
    assert "'port': '2'" in out


def test_switch_table_dump_decode_failure_falls_back(network: MagicMock) -> None:
    """If decode_match raises (e.g. corrupt entry), render the raw dict."""
    network.switches["s1"].client.list_table_entries = MagicMock(
        return_value=[
            {
                "table": "MyIngress.ipv4_lpm",
                # Wrong shape for LPM (bytes instead of (bytes, plen)) -> raises.
                "match": {"hdr.ipv4.dstAddr": b"\x0a\x00\x00\x00"},
                "action": "NoAction",
                "params": {},
                "priority": None,
            }
        ]
    )
    d = CommandDispatcher(network)
    out = d.dispatch("s1 table dump MyIngress.ipv4_lpm")
    # Raw fallback path keeps the dump from failing.
    assert "MyIngress.ipv4_lpm" in out
    assert "NoAction" in out


# ---------------------------------------------------------------------------
# table add
# ---------------------------------------------------------------------------


def test_table_add_full_form(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.insert_table_entry = MagicMock(return_value=None)
    d = CommandDispatcher(network)
    out = d.dispatch(
        "s1 table add MyIngress.ipv4_lpm "
        "match: hdr.ipv4.dstAddr=10.0.0.0/24 "
        "action: MyIngress.set_egress_port "
        "params: port=2"
    )
    assert out == "ok"
    args, _ = sw.client.insert_table_entry.call_args
    assert args[0] == "MyIngress.ipv4_lpm"
    assert args[1] == {"hdr.ipv4.dstAddr": "10.0.0.0/24"}
    assert args[2] == "MyIngress.set_egress_port"
    assert args[3] == {"port": "2"}


def test_table_add_with_priority(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.insert_table_entry = MagicMock(return_value=None)
    d = CommandDispatcher(network)
    d.dispatch(
        "s1 table add MyIngress.ipv4_lpm "
        "match: hdr.ipv4.dstAddr=10.0.0.0/24 "
        "action: NoAction priority: 100"
    )
    _, kwargs = sw.client.insert_table_entry.call_args
    assert kwargs == {"priority": 100}


def test_table_add_without_action_raises(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    with pytest.raises(CLIUsageError, match="'action:' section is required"):
        d.dispatch("s1 table add MyIngress.ipv4_lpm match: a=1")


def test_table_add_without_match_raises(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    with pytest.raises(CLIUsageError, match="'match:' section is required"):
        d.dispatch("s1 table add MyIngress.ipv4_lpm action: NoAction")


def test_table_add_unknown_section_raises(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    with pytest.raises(CLIUsageError, match="unknown section marker"):
        d.dispatch("s1 table add MyIngress.ipv4_lpm match: a=1 action: NoAction nope: bad")


def test_table_add_renders_client_error(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.insert_table_entry = MagicMock(side_effect=RuntimeError("boom"))
    d = CommandDispatcher(network)
    out = d.dispatch("s1 table add MyIngress.ipv4_lpm match: a=1 action: NoAction")
    assert "error:" in out
    assert "boom" in out


def test_table_add_multiple_match_pairs(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.insert_table_entry = MagicMock(return_value=None)
    d = CommandDispatcher(network)
    d.dispatch(
        "s1 table add MyIngress.ipv4_lpm "
        "match: hdr.eth.src=00:11:22:33:44:55,hdr.eth.dst=aa:bb:cc:dd:ee:ff "
        "action: NoAction"
    )
    args, _ = sw.client.insert_table_entry.call_args
    assert args[1] == {
        "hdr.eth.src": "00:11:22:33:44:55",
        "hdr.eth.dst": "aa:bb:cc:dd:ee:ff",
    }


# ---------------------------------------------------------------------------
# table del / clear
# ---------------------------------------------------------------------------


def test_table_del(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.delete_table_entry = MagicMock(return_value=None)
    d = CommandDispatcher(network)
    out = d.dispatch("s1 table del MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.0/24")
    assert out == "ok"
    args, _ = sw.client.delete_table_entry.call_args
    assert args[0] == "MyIngress.ipv4_lpm"
    assert args[1] == {"hdr.ipv4.dstAddr": "10.0.0.0/24"}


def test_table_del_with_priority(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.delete_table_entry = MagicMock(return_value=None)
    d = CommandDispatcher(network)
    d.dispatch("s1 table del t match: a=1 priority: 50")
    _, kwargs = sw.client.delete_table_entry.call_args
    assert kwargs == {"priority": 50}


def test_table_clear(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.clear_table = MagicMock(return_value=3)
    d = CommandDispatcher(network)
    out = d.dispatch("s1 table clear MyIngress.ipv4_lpm")
    assert out == "cleared 3 entries"


# ---------------------------------------------------------------------------
# counter
# ---------------------------------------------------------------------------


def test_counter_single_index_renders_one_line(network: MagicMock) -> None:
    from p4net.control import CounterData

    sw = network.switches["s1"]
    sw.client.read_counter = MagicMock(return_value=CounterData(7, 700))
    d = CommandDispatcher(network)
    out = d.dispatch("s1 counter MyIngress.ingress_pkts 0")
    assert out == "pkts=7 bytes=700"
    args, _ = sw.client.read_counter.call_args
    assert args == ("MyIngress.ingress_pkts", 0)


def test_counter_no_index_renders_table(network: MagicMock) -> None:
    from p4net.control import CounterData

    sw = network.switches["s1"]
    sw.client.read_counter = MagicMock(return_value={0: CounterData(1, 64), 5: CounterData(2, 128)})
    d = CommandDispatcher(network)
    out = d.dispatch("s1 counter MyIngress.ingress_pkts")
    assert "index" in out
    assert "0" in out
    assert "5" in out
    assert "64" in out
    assert "128" in out


def test_counter_no_index_empty(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.read_counter = MagicMock(return_value={})
    d = CommandDispatcher(network)
    out = d.dispatch("s1 counter MyIngress.ingress_pkts")
    assert "no populated cells" in out


def test_counter_reset_with_index(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.reset_counter = MagicMock(return_value=None)
    d = CommandDispatcher(network)
    out = d.dispatch("s1 counter reset MyIngress.ingress_pkts 0")
    assert out == "ok"
    args, _ = sw.client.reset_counter.call_args
    assert args == ("MyIngress.ingress_pkts", 0)


def test_counter_reset_without_index(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.reset_counter = MagicMock(return_value=None)
    d = CommandDispatcher(network)
    out = d.dispatch("s1 counter reset MyIngress.ingress_pkts")
    assert out == "ok"
    args, _ = sw.client.reset_counter.call_args
    assert args == ("MyIngress.ingress_pkts",)


def test_counter_missing_name(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    with pytest.raises(CLIUsageError, match="missing counter name"):
        d.dispatch("s1 counter")


# ---------------------------------------------------------------------------
# mcast
# ---------------------------------------------------------------------------


def test_mcast_add(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.add_multicast_group = MagicMock(return_value=None)
    d = CommandDispatcher(network)
    out = d.dispatch("s1 mcast add 5 1,2,3")
    assert out == "ok"
    args, _ = sw.client.add_multicast_group.call_args
    assert args == (5, [1, 2, 3])


def test_mcast_add_invalid_id(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    with pytest.raises(CLIUsageError, match="id must be an integer"):
        d.dispatch("s1 mcast add abc 1,2")


def test_mcast_add_invalid_ports(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    with pytest.raises(CLIUsageError, match="ports must be"):
        d.dispatch("s1 mcast add 1 a,b")


def test_mcast_del(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.delete_multicast_group = MagicMock(return_value=None)
    d = CommandDispatcher(network)
    out = d.dispatch("s1 mcast del 7")
    assert out == "ok"
    args, _ = sw.client.delete_multicast_group.call_args
    assert args == (7,)


def test_mcast_list(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.list_multicast_groups = MagicMock(return_value={1: [1, 2, 3], 2: [4]})
    d = CommandDispatcher(network)
    out = d.dispatch("s1 mcast list")
    assert "1: [1, 2, 3]" in out
    assert "2: [4]" in out


def test_mcast_list_empty(network: MagicMock) -> None:
    sw = network.switches["s1"]
    sw.client.list_multicast_groups = MagicMock(return_value={})
    d = CommandDispatcher(network)
    out = d.dispatch("s1 mcast list")
    assert "no multicast groups" in out


def test_mcast_unknown_subverb(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    with pytest.raises(CLIUsageError, match="unknown sub-verb"):
        d.dispatch("s1 mcast nope")


# ---------------------------------------------------------------------------
# Switch top-level dispatch
# ---------------------------------------------------------------------------


def test_switch_unknown_verb(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    with pytest.raises(CLIUsageError, match="unknown verb"):
        d.dispatch("s1 fly")


def test_switch_missing_verb(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    with pytest.raises(CLIUsageError, match="missing verb"):
        d.dispatch("s1")


def test_table_names_for_completion(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    names = d.table_names_for("s1")
    assert "MyIngress.ipv4_lpm" in names


def test_table_names_for_unknown_returns_empty(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    assert d.table_names_for("nope") == []


# ---------------------------------------------------------------------------
# Help registry includes the new switch topics
# ---------------------------------------------------------------------------


def test_help_lists_switch_commands(network: MagicMock) -> None:
    d = CommandDispatcher(network)
    out = d.dispatch("help")
    for topic in (
        "<switch> log",
        "<switch> table list",
        "<switch> table add",
        "<switch> counter",
        "<switch> mcast list",
    ):
        assert topic in out


_KEEP_ALIVE: type = Any  # keep `Any` import live; used implicitly above
