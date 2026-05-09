"""Unit tests for `p4net.control.p4info_index`. Builds P4Info programmatically."""

from __future__ import annotations

import pytest
from p4.config.v1 import p4info_pb2

# Importing p4net.control first sets the protobuf python-impl env var so the
# bundled p4 stubs load on protobuf>=5.
import p4net.control  # noqa: F401
from p4net.control import (
    EncodingError,
    NoSuchActionError,
    NoSuchFieldError,
    NoSuchTableError,
    P4InfoIndex,
)

# ---------------------------------------------------------------------------
# Fixture: a small P4Info covering EXACT, LPM, TERNARY, RANGE, OPTIONAL.
# ---------------------------------------------------------------------------


@pytest.fixture
def p4info() -> p4info_pb2.P4Info:
    p = p4info_pb2.P4Info()

    # Action: NoAction
    a_no = p.actions.add()
    a_no.preamble.id = 1001
    a_no.preamble.name = "NoAction"

    # Action: set_egress_port(port: bit<9>)
    a_set = p.actions.add()
    a_set.preamble.id = 1002
    a_set.preamble.name = "MyIngress.set_egress_port"
    pa = a_set.params.add()
    pa.id = 1
    pa.name = "port"
    pa.bitwidth = 9

    # Action: drop()
    a_drop = p.actions.add()
    a_drop.preamble.id = 1003
    a_drop.preamble.name = "MyIngress.drop"

    # Table: ipv4_lpm — LPM on dstAddr (32 bits)
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
    t_lpm.action_refs.add().id = 1003
    t_lpm.size = 1024

    # Table: exact_acl — EXACT on src and dst MAC
    t_exact = p.tables.add()
    t_exact.preamble.id = 2002
    t_exact.preamble.name = "MyIngress.exact_acl"
    mf1 = t_exact.match_fields.add()
    mf1.id = 1
    mf1.name = "hdr.ethernet.srcAddr"
    mf1.bitwidth = 48
    mf1.match_type = p4info_pb2.MatchField.EXACT
    mf2 = t_exact.match_fields.add()
    mf2.id = 2
    mf2.name = "hdr.ethernet.dstAddr"
    mf2.bitwidth = 48
    mf2.match_type = p4info_pb2.MatchField.EXACT
    t_exact.action_refs.add().id = 1001

    # Table: ternary_acl — TERNARY on dstAddr
    t_tern = p.tables.add()
    t_tern.preamble.id = 2003
    t_tern.preamble.name = "MyIngress.ternary_acl"
    mft = t_tern.match_fields.add()
    mft.id = 1
    mft.name = "hdr.ipv4.dstAddr"
    mft.bitwidth = 32
    mft.match_type = p4info_pb2.MatchField.TERNARY

    # Table: range_acl — RANGE on srcPort
    t_range = p.tables.add()
    t_range.preamble.id = 2004
    t_range.preamble.name = "MyIngress.range_acl"
    mfr = t_range.match_fields.add()
    mfr.id = 1
    mfr.name = "hdr.tcp.srcPort"
    mfr.bitwidth = 16
    mfr.match_type = p4info_pb2.MatchField.RANGE

    # Table: optional_acl — OPTIONAL on flags
    t_opt = p.tables.add()
    t_opt.preamble.id = 2005
    t_opt.preamble.name = "MyIngress.optional_acl"
    mfo = t_opt.match_fields.add()
    mfo.id = 1
    mfo.name = "hdr.tcp.flags"
    mfo.bitwidth = 8
    mfo.match_type = p4info_pb2.MatchField.OPTIONAL

    # Counter
    c = p.counters.add()
    c.preamble.id = 3001
    c.preamble.name = "MyIngress.ingress_pkts"

    return p


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_table_action_counter_id(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    assert idx.table_id("MyIngress.ipv4_lpm") == 2001
    assert idx.action_id("MyIngress.set_egress_port") == 1002
    assert idx.counter_id("MyIngress.ingress_pkts") == 3001


def test_lookup_unknown_raises(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    with pytest.raises(NoSuchTableError):
        idx.table_id("nope")
    with pytest.raises(NoSuchActionError):
        idx.action_id("nope")


def test_inverse_lookups(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    assert idx.table_name(2001) == "MyIngress.ipv4_lpm"
    assert idx.action_name(1002) == "MyIngress.set_egress_port"
    assert idx.counter_name(3001) == "MyIngress.ingress_pkts"
    with pytest.raises(NoSuchTableError):
        idx.table_name(999999)


def test_table_requires_priority(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    assert idx.table_requires_priority("MyIngress.ipv4_lpm") is False
    assert idx.table_requires_priority("MyIngress.exact_acl") is False
    assert idx.table_requires_priority("MyIngress.ternary_acl") is True
    assert idx.table_requires_priority("MyIngress.range_acl") is True
    assert idx.table_requires_priority("MyIngress.optional_acl") is False


def test_table_names_and_action_names(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    assert "MyIngress.ipv4_lpm" in idx.table_names
    assert "MyIngress.set_egress_port" in idx.action_names


# ---------------------------------------------------------------------------
# encode_match
# ---------------------------------------------------------------------------


def test_encode_match_lpm_string(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    fms = idx.encode_match("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.1.0/24"})
    assert len(fms) == 1
    fm = fms[0]
    assert fm.field_id == 1
    assert fm.HasField("lpm")
    # canonical = lstrip leading 0x00 bytes only; "10.0.1.0" has no leading zeros
    assert fm.lpm.value == b"\x0a\x00\x01\x00"
    assert fm.lpm.prefix_len == 24


def test_encode_match_lpm_tuple(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    fms = idx.encode_match("MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": ("10.0.0.0", 16)})
    assert fms[0].lpm.prefix_len == 16


def test_encode_match_exact_two_fields(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    fms = idx.encode_match(
        "MyIngress.exact_acl",
        {
            "hdr.ethernet.srcAddr": "aa:bb:cc:dd:ee:01",
            "hdr.ethernet.dstAddr": "aa:bb:cc:dd:ee:02",
        },
    )
    assert len(fms) == 2
    by_id = {fm.field_id: fm for fm in fms}
    assert by_id[1].HasField("exact")
    assert by_id[2].HasField("exact")


def test_encode_match_exact_missing_field_raises(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    with pytest.raises(EncodingError, match="required exact match field"):
        idx.encode_match(
            "MyIngress.exact_acl",
            {"hdr.ethernet.srcAddr": "aa:bb:cc:dd:ee:01"},
        )


def test_encode_match_ternary(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    fms = idx.encode_match(
        "MyIngress.ternary_acl",
        {"hdr.ipv4.dstAddr": ("10.0.0.0", "255.255.0.0")},
    )
    assert len(fms) == 1
    assert fms[0].HasField("ternary")
    # canonical strips leading 0x00 only; "10.0.0.0" has no leading zeros
    assert fms[0].ternary.value == b"\x0a\x00\x00\x00"
    assert fms[0].ternary.mask == b"\xff\xff\x00\x00"


def test_encode_match_ternary_zero_mask_omitted(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    fms = idx.encode_match("MyIngress.ternary_acl", {"hdr.ipv4.dstAddr": ("10.0.0.0", "0.0.0.0")})
    assert fms == []


def test_encode_match_range(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    fms = idx.encode_match("MyIngress.range_acl", {"hdr.tcp.srcPort": (1024, 65535)})
    assert fms[0].HasField("range")


def test_encode_match_optional(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    fms = idx.encode_match("MyIngress.optional_acl", {"hdr.tcp.flags": 0x18})
    assert fms[0].HasField("optional")


def test_encode_match_unknown_field(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    with pytest.raises(NoSuchFieldError):
        idx.encode_match("MyIngress.ipv4_lpm", {"bogus": "10.0.0.0/24"})


def test_encode_match_unknown_table(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    with pytest.raises(NoSuchTableError):
        idx.encode_match("nope", {})


# ---------------------------------------------------------------------------
# encode_action
# ---------------------------------------------------------------------------


def test_encode_action_zero_arity(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    a = idx.encode_action("MyIngress.drop")
    assert a.action_id == 1003
    assert list(a.params) == []


def test_encode_action_zero_arity_with_empty_dict(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    a = idx.encode_action("MyIngress.drop", {})
    assert a.action_id == 1003


def test_encode_action_with_params(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    a = idx.encode_action("MyIngress.set_egress_port", {"port": 2})
    assert a.action_id == 1002
    assert len(a.params) == 1
    assert a.params[0].param_id == 1
    assert a.params[0].value == b"\x02"


def test_encode_action_unknown_action(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    with pytest.raises(NoSuchActionError):
        idx.encode_action("nope")


def test_encode_action_unknown_param(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    with pytest.raises(NoSuchFieldError):
        idx.encode_action("MyIngress.set_egress_port", {"bogus": 1})


def test_encode_action_missing_required_param(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    with pytest.raises(EncodingError, match="required action param"):
        idx.encode_action("MyIngress.set_egress_port", {})


def test_encode_action_param_overflow(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    with pytest.raises(EncodingError):
        idx.encode_action("MyIngress.set_egress_port", {"port": 1024})  # 9-bit max 511


# ---------------------------------------------------------------------------
# from_bytes (text protobuf round-trip)
# ---------------------------------------------------------------------------


def test_from_bytes_text_protobuf_round_trip(p4info: p4info_pb2.P4Info) -> None:
    from google.protobuf import text_format

    text = text_format.MessageToString(p4info)
    idx = P4InfoIndex.from_bytes(text.encode("utf-8"), text_format=True)
    assert idx.table_id("MyIngress.ipv4_lpm") == 2001


def test_from_bytes_binary_round_trip(p4info: p4info_pb2.P4Info) -> None:
    binary = p4info.SerializeToString()
    idx = P4InfoIndex.from_bytes(binary, text_format=False)
    assert idx.table_id("MyIngress.ipv4_lpm") == 2001
