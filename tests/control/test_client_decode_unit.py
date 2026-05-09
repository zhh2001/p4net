"""Unit tests for `P4RuntimeClient._decode_table_entry` and `clear_table`.

These hand-construct `p4.v1.TableEntry` protos for every match type and feed
them through the decoder, plus stage a multi-entry `clear_table` to assert
one batched WriteRequest with the expected number of DELETE updates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2

import p4net.control  # ensures protobuf python-impl env var is set  # noqa: F401
from p4net.control import P4InfoIndex, P4RuntimeClient


def _build_p4info_all_match_types() -> p4info_pb2.P4Info:
    p = p4info_pb2.P4Info()

    # Action: NoAction
    a_no = p.actions.add()
    a_no.preamble.id = 1001
    a_no.preamble.name = "NoAction"

    # Action: act_with_param(p: bit<8>)
    a_set = p.actions.add()
    a_set.preamble.id = 1002
    a_set.preamble.name = "MyIngress.act_with_param"
    pa = a_set.params.add()
    pa.id = 1
    pa.name = "p"
    pa.bitwidth = 8

    # Tables: one per match type.
    def add_table(tid: int, name: str, mf_name: str, bitwidth: int, mt: Any) -> None:
        t = p.tables.add()
        t.preamble.id = tid
        t.preamble.name = name
        mf = t.match_fields.add()
        mf.id = 1
        mf.name = mf_name
        mf.bitwidth = bitwidth
        mf.match_type = mt
        t.action_refs.add().id = 1001
        t.action_refs.add().id = 1002

    add_table(2001, "MyIngress.exact_t", "f_exact", 8, p4info_pb2.MatchField.EXACT)
    add_table(2002, "MyIngress.lpm_t", "f_lpm", 32, p4info_pb2.MatchField.LPM)
    add_table(2003, "MyIngress.tern_t", "f_tern", 32, p4info_pb2.MatchField.TERNARY)
    add_table(2004, "MyIngress.range_t", "f_range", 16, p4info_pb2.MatchField.RANGE)
    add_table(2005, "MyIngress.opt_t", "f_opt", 16, p4info_pb2.MatchField.OPTIONAL)
    return p


def _make_client_with_index() -> P4RuntimeClient:
    """Construct a client without opening any gRPC channel and inject the index."""
    c = P4RuntimeClient("127.0.0.1:50051", device_id=0)
    c._index = P4InfoIndex(_build_p4info_all_match_types())
    return c


# ---------------------------------------------------------------------------
# Decoder coverage for every match type
# ---------------------------------------------------------------------------


class TestDecodeTableEntry:
    def test_exact(self) -> None:
        c = _make_client_with_index()
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = 2001
        fm = entry.match.add()
        fm.field_id = 1
        fm.exact.value = b"\x42"
        entry.action.action.action_id = 1002
        ap = entry.action.action.params.add()
        ap.param_id = 1
        ap.value = b"\x07"
        decoded = c._decode_table_entry(entry)
        assert decoded["table"] == "MyIngress.exact_t"
        assert decoded["action"] == "MyIngress.act_with_param"
        assert decoded["params"] == {"p": b"\x07"}
        assert decoded["match"] == {"f_exact": b"\x42"}
        assert decoded["priority"] is None

    def test_lpm(self) -> None:
        c = _make_client_with_index()
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = 2002
        fm = entry.match.add()
        fm.field_id = 1
        fm.lpm.value = b"\x0a\x00\x01\x00"
        fm.lpm.prefix_len = 24
        entry.action.action.action_id = 1001
        decoded = c._decode_table_entry(entry)
        assert decoded["table"] == "MyIngress.lpm_t"
        assert decoded["action"] == "NoAction"
        assert decoded["match"] == {"f_lpm": (b"\x0a\x00\x01\x00", 24)}

    def test_ternary(self) -> None:
        c = _make_client_with_index()
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = 2003
        fm = entry.match.add()
        fm.field_id = 1
        fm.ternary.value = b"\x0a\x00"
        fm.ternary.mask = b"\xff\xff"
        entry.priority = 100
        entry.action.action.action_id = 1001
        decoded = c._decode_table_entry(entry)
        assert decoded["table"] == "MyIngress.tern_t"
        assert decoded["match"] == {"f_tern": (b"\x0a\x00", b"\xff\xff")}
        assert decoded["priority"] == 100

    def test_range(self) -> None:
        c = _make_client_with_index()
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = 2004
        fm = entry.match.add()
        fm.field_id = 1
        fm.range.low = b"\x04\x00"
        fm.range.high = b"\x10\x00"
        entry.priority = 50
        entry.action.action.action_id = 1001
        decoded = c._decode_table_entry(entry)
        assert decoded["match"] == {"f_range": (b"\x04\x00", b"\x10\x00")}
        assert decoded["priority"] == 50

    def test_optional(self) -> None:
        c = _make_client_with_index()
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = 2005
        fm = entry.match.add()
        fm.field_id = 1
        fm.optional.value = b"\xab"
        entry.action.action.action_id = 1001
        decoded = c._decode_table_entry(entry)
        assert decoded["match"] == {"f_opt": b"\xab"}

    def test_unknown_field_id_is_skipped(self) -> None:
        c = _make_client_with_index()
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = 2001
        fm = entry.match.add()
        fm.field_id = 9999  # not present in the table
        fm.exact.value = b"\x00"
        entry.action.action.action_id = 1001
        decoded = c._decode_table_entry(entry)
        assert decoded["match"] == {}

    def test_no_action(self) -> None:
        c = _make_client_with_index()
        entry = p4runtime_pb2.TableEntry()
        entry.table_id = 2001
        fm = entry.match.add()
        fm.field_id = 1
        fm.exact.value = b"\x01"
        # Don't set action; the decoder should yield action=None and empty params.
        decoded = c._decode_table_entry(entry)
        assert decoded["action"] is None
        assert decoded["params"] == {}


# ---------------------------------------------------------------------------
# clear_table batches DELETE updates
# ---------------------------------------------------------------------------


def _connected_client_with_lpm(tmp_path: Path) -> P4RuntimeClient:
    """Manually inject internal state to simulate a connected client."""
    c = P4RuntimeClient("127.0.0.1:50051", device_id=0)
    c._index = P4InfoIndex(_build_p4info_all_match_types())
    c._connected = True
    c._stub = MagicMock()
    return c


def test_clear_table_batches_all_deletes(tmp_path: Path) -> None:
    c = _connected_client_with_lpm(tmp_path)
    # Build 5 fake LPM entries the Read should return.
    resp = p4runtime_pb2.ReadResponse()
    for i in range(5):
        ent = resp.entities.add()
        te = ent.table_entry
        te.table_id = 2002
        fm = te.match.add()
        fm.field_id = 1
        fm.lpm.value = bytes([10, 0, i, 0]).rstrip(b"\x00") or b"\x00"
        fm.lpm.prefix_len = 24
    # Read returns the response stream; Write captures the request.
    c._stub.Read = MagicMock(return_value=iter([resp]))
    c._stub.Write = MagicMock(return_value=MagicMock())

    n = c.clear_table("MyIngress.lpm_t")
    assert n == 5
    # Exactly one WriteRequest with 5 DELETE updates targeting table 2002.
    assert c._stub.Write.call_count == 1
    sent = c._stub.Write.call_args.args[0]
    assert len(sent.updates) == 5
    delete_enum = p4runtime_pb2.Update.Type.Value("DELETE")
    for upd in sent.updates:
        assert upd.type == delete_enum
        assert upd.entity.table_entry.table_id == 2002


def test_clear_table_returns_zero_when_empty(tmp_path: Path) -> None:
    c = _connected_client_with_lpm(tmp_path)
    c._stub.Read = MagicMock(return_value=iter([p4runtime_pb2.ReadResponse()]))
    c._stub.Write = MagicMock(return_value=MagicMock())
    assert c.clear_table("MyIngress.lpm_t") == 0
    c._stub.Write.assert_not_called()


# ---------------------------------------------------------------------------
# Property accessors (cheap coverage)
# ---------------------------------------------------------------------------


def test_property_accessors() -> None:
    c = P4RuntimeClient("host:1234", device_id=42, election_id=(7, 8), role="r")
    assert c.target == "host:1234"
    assert c.device_id == 42
    assert c.election_id == (7, 8)
    assert c.is_connected() is False
    from p4net.control import P4RuntimeError

    with pytest.raises(P4RuntimeError):
        _ = c.index  # raises P4RuntimeError before pipeline is set
