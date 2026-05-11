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


# ---------------------------------------------------------------------------
# decode_match — round-trip from encode_match
# ---------------------------------------------------------------------------


def _decoded_match_for(p4info: p4info_pb2.P4Info, table: str, raw: dict) -> dict:
    """Encode then decode by walking the FieldMatch protos back into raw form."""
    idx = P4InfoIndex(p4info)
    fms = idx.encode_match(table, raw)
    table_msg = next(t for t in p4info.tables if t.preamble.name == table)
    fields_by_id = {int(mf.id): mf for mf in table_msg.match_fields}
    decoded_raw: dict = {}
    for fm in fms:
        mf = fields_by_id[fm.field_id]
        if fm.HasField("exact"):
            decoded_raw[mf.name] = bytes(fm.exact.value)
        elif fm.HasField("lpm"):
            decoded_raw[mf.name] = (bytes(fm.lpm.value), int(fm.lpm.prefix_len))
        elif fm.HasField("ternary"):
            decoded_raw[mf.name] = (bytes(fm.ternary.value), bytes(fm.ternary.mask))
        elif fm.HasField("range"):
            decoded_raw[mf.name] = (bytes(fm.range.low), bytes(fm.range.high))
        elif fm.HasField("optional"):
            decoded_raw[mf.name] = bytes(fm.optional.value)
    return idx.decode_match(table, decoded_raw)


def test_decode_match_lpm_round_trip(p4info: p4info_pb2.P4Info) -> None:
    out = _decoded_match_for(p4info, "MyIngress.ipv4_lpm", {"hdr.ipv4.dstAddr": "10.0.0.5/24"})
    assert out == {"hdr.ipv4.dstAddr": "10.0.0.5/24"}


def test_decode_match_exact_ipv4_no_table() -> None:
    # Construct a small P4Info with an EXACT IPv4 field for a focused test.
    p = p4info_pb2.P4Info()
    a = p.actions.add()
    a.preamble.id = 1
    a.preamble.name = "NoAction"
    t = p.tables.add()
    t.preamble.id = 10
    t.preamble.name = "t"
    mf = t.match_fields.add()
    mf.id = 1
    mf.name = "ipv4"
    mf.bitwidth = 32
    mf.match_type = p4info_pb2.MatchField.EXACT
    t.action_refs.add().id = 1
    out = _decoded_match_for(p, "t", {"ipv4": "10.0.0.5"})
    assert out == {"ipv4": "10.0.0.5"}


def test_decode_match_exact_mac(p4info: p4info_pb2.P4Info) -> None:
    out = _decoded_match_for(
        p4info,
        "MyIngress.exact_acl",
        {
            "hdr.ethernet.srcAddr": "aa:bb:cc:dd:ee:01",
            "hdr.ethernet.dstAddr": "aa:bb:cc:dd:ee:02",
        },
    )
    assert out["hdr.ethernet.srcAddr"] == "aa:bb:cc:dd:ee:01"
    assert out["hdr.ethernet.dstAddr"] == "aa:bb:cc:dd:ee:02"


def test_decode_match_exact_int_no_table() -> None:
    # 16-bit EXACT field
    p = p4info_pb2.P4Info()
    a = p.actions.add()
    a.preamble.id = 1
    a.preamble.name = "NoAction"
    t = p.tables.add()
    t.preamble.id = 11
    t.preamble.name = "t"
    mf = t.match_fields.add()
    mf.id = 1
    mf.name = "n"
    mf.bitwidth = 16
    mf.match_type = p4info_pb2.MatchField.EXACT
    t.action_refs.add().id = 1
    out = _decoded_match_for(p, "t", {"n": 42})
    assert out == {"n": "42"}


def test_decode_match_ternary_round_trip(p4info: p4info_pb2.P4Info) -> None:
    out = _decoded_match_for(
        p4info,
        "MyIngress.ternary_acl",
        {"hdr.ipv4.dstAddr": ("10.0.0.0", "255.255.0.0")},
    )
    assert out == {"hdr.ipv4.dstAddr": "10.0.0.0&255.255.0.0"}


def test_decode_match_range_round_trip(p4info: p4info_pb2.P4Info) -> None:
    out = _decoded_match_for(
        p4info,
        "MyIngress.range_acl",
        {"hdr.tcp.srcPort": (1024, 65535)},
    )
    assert out == {"hdr.tcp.srcPort": "[1024,65535]"}


def test_decode_match_optional_round_trip(p4info: p4info_pb2.P4Info) -> None:
    out = _decoded_match_for(p4info, "MyIngress.optional_acl", {"hdr.tcp.flags": 0x18})
    assert out == {"hdr.tcp.flags": "24"}


def test_decode_match_unknown_table(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    with pytest.raises(NoSuchTableError):
        idx.decode_match("nope", {})


def test_decode_match_unknown_field(p4info: p4info_pb2.P4Info) -> None:
    idx = P4InfoIndex(p4info)
    with pytest.raises(NoSuchFieldError):
        idx.decode_match("MyIngress.ipv4_lpm", {"bogus": (b"\n", 8)})


def test_from_bytes_text_protobuf_round_trip(p4info: p4info_pb2.P4Info) -> None:
    from google.protobuf import text_format

    text = text_format.MessageToString(p4info)
    idx = P4InfoIndex.from_bytes(text.encode("utf-8"), text_format=True)
    assert idx.table_id("MyIngress.ipv4_lpm") == 2001


def test_from_bytes_binary_round_trip(p4info: p4info_pb2.P4Info) -> None:
    binary = p4info.SerializeToString()
    idx = P4InfoIndex.from_bytes(binary, text_format=False)
    assert idx.table_id("MyIngress.ipv4_lpm") == 2001


# ---------------------------------------------------------------------------
# Controller packet metadata
# ---------------------------------------------------------------------------


def _add_controller_metadata(
    p: p4info_pb2.P4Info,
    name: str,
    fields: list[tuple[int, str, int]],
    *,
    preamble_id: int,
) -> p4info_pb2.ControllerPacketMetadata:
    cpm = p.controller_packet_metadata.add()
    cpm.preamble.id = preamble_id
    cpm.preamble.name = name
    for fid, fname, bw in fields:
        m = cpm.metadata.add()
        m.id = fid
        m.name = fname
        m.bitwidth = bw
    return cpm


@pytest.fixture
def p4info_with_controller_metadata() -> p4info_pb2.P4Info:
    p = p4info_pb2.P4Info()
    _add_controller_metadata(
        p,
        "packet_in",
        [(1, "ingress_port", 9), (2, "_pad0", 7)],
        preamble_id=80000001,
    )
    _add_controller_metadata(
        p,
        "packet_out",
        [(1, "egress_port", 9), (2, "_pad0", 7)],
        preamble_id=80000002,
    )
    return p


class TestControllerPacketMetadata:
    def test_packet_in_schema(self, p4info_with_controller_metadata: p4info_pb2.P4Info) -> None:
        idx = P4InfoIndex(p4info_with_controller_metadata)
        assert idx.packet_in_metadata_schema() == [("ingress_port", 9), ("_pad0", 7)]

    def test_packet_out_schema(self, p4info_with_controller_metadata: p4info_pb2.P4Info) -> None:
        idx = P4InfoIndex(p4info_with_controller_metadata)
        assert idx.packet_out_metadata_schema() == [("egress_port", 9), ("_pad0", 7)]

    def test_encode_packet_out_metadata_basic(
        self, p4info_with_controller_metadata: p4info_pb2.P4Info
    ) -> None:
        idx = P4InfoIndex(p4info_with_controller_metadata)
        out = idx.encode_packet_out_metadata({"egress_port": 1})
        assert len(out) == 2
        by_id = {pm.metadata_id: pm for pm in out}
        assert by_id[1].value == b"\x01"
        # Missing key -> auto-zero (canonical \x00).
        assert by_id[2].value == b"\x00"

    def test_encode_packet_out_unknown_field(
        self, p4info_with_controller_metadata: p4info_pb2.P4Info
    ) -> None:
        idx = P4InfoIndex(p4info_with_controller_metadata)
        with pytest.raises(NoSuchFieldError):
            idx.encode_packet_out_metadata({"bogus": 0})

    def test_encode_packet_out_overflow(
        self, p4info_with_controller_metadata: p4info_pb2.P4Info
    ) -> None:
        idx = P4InfoIndex(p4info_with_controller_metadata)
        with pytest.raises(EncodingError):
            idx.encode_packet_out_metadata({"egress_port": 1024})  # 9-bit max 511

    def test_decode_packet_in_metadata_round_trip(
        self, p4info_with_controller_metadata: p4info_pb2.P4Info
    ) -> None:
        from p4.v1 import p4runtime_pb2

        idx = P4InfoIndex(p4info_with_controller_metadata)
        msgs = []
        pm = p4runtime_pb2.PacketMetadata()
        pm.metadata_id = 1
        pm.value = b"\x05"
        msgs.append(pm)
        pm2 = p4runtime_pb2.PacketMetadata()
        pm2.metadata_id = 2
        pm2.value = b"\x00"
        msgs.append(pm2)
        out = idx.decode_packet_in_metadata(msgs)
        assert out == {"ingress_port": 5, "_pad0": 0}

    def test_decode_packet_in_unknown_id_dropped(
        self, p4info_with_controller_metadata: p4info_pb2.P4Info
    ) -> None:
        from p4.v1 import p4runtime_pb2

        idx = P4InfoIndex(p4info_with_controller_metadata)
        pm = p4runtime_pb2.PacketMetadata()
        pm.metadata_id = 999  # not in schema
        pm.value = b"\x01"
        out = idx.decode_packet_in_metadata([pm])
        assert out == {}

    def test_no_controller_headers_returns_empty(self) -> None:
        # Use a P4Info with no controller_packet_metadata.
        p = p4info_pb2.P4Info()
        idx = P4InfoIndex(p)
        assert idx.packet_in_metadata_schema() == []
        assert idx.packet_out_metadata_schema() == []
        assert idx.encode_packet_out_metadata({}) == []
        assert idx.decode_packet_in_metadata([]) == {}

    def test_encode_when_no_packet_out_header_with_metadata_raises(self) -> None:
        p = p4info_pb2.P4Info()
        idx = P4InfoIndex(p)
        with pytest.raises(NoSuchFieldError):
            idx.encode_packet_out_metadata({"egress_port": 1})


# ---------------------------------------------------------------------------
# IPv6 LPM round-trip (phase 13)
# ---------------------------------------------------------------------------


def _build_p4info_with_ipv6_lpm() -> p4info_pb2.P4Info:
    p = p4info_pb2.P4Info()
    a = p.actions.add()
    a.preamble.id = 1001
    a.preamble.name = "NoAction"
    t = p.tables.add()
    t.preamble.id = 2001
    t.preamble.name = "MyIngress.ipv6_lpm"
    mf = t.match_fields.add()
    mf.id = 1
    mf.name = "hdr.ipv6.dstAddr"
    mf.bitwidth = 128
    mf.match_type = p4info_pb2.MatchField.LPM
    t.action_refs.add().id = 1001
    return p


def test_encode_decode_match_round_trip_ipv6_lpm() -> None:
    idx = P4InfoIndex(_build_p4info_with_ipv6_lpm())
    fms = idx.encode_match("MyIngress.ipv6_lpm", {"hdr.ipv6.dstAddr": "fd00::1/128"})
    assert len(fms) == 1
    fm = fms[0]
    assert fm.HasField("lpm")
    # Walk back into the raw shape that list_table_entries produces, then
    # decode_match — that's the round-trip the CLI relies on.
    raw = {"hdr.ipv6.dstAddr": (bytes(fm.lpm.value), int(fm.lpm.prefix_len))}
    out = idx.decode_match("MyIngress.ipv6_lpm", raw)
    assert out == {"hdr.ipv6.dstAddr": "fd00::1/128"}


def test_encode_decode_match_round_trip_ipv6_lpm_subnet() -> None:
    idx = P4InfoIndex(_build_p4info_with_ipv6_lpm())
    fms = idx.encode_match("MyIngress.ipv6_lpm", {"hdr.ipv6.dstAddr": "fd00::/64"})
    fm = fms[0]
    raw = {"hdr.ipv6.dstAddr": (bytes(fm.lpm.value), int(fm.lpm.prefix_len))}
    out = idx.decode_match("MyIngress.ipv6_lpm", raw)
    assert out == {"hdr.ipv6.dstAddr": "fd00::/64"}


def _build_p4info_with_register(
    *,
    name: str = "MyIngress.test_register",
    register_id: int = 3001,
    bitwidth: int = 32,
    size: int = 256,
) -> p4info_pb2.P4Info:
    p = p4info_pb2.P4Info()
    r = p.registers.add()
    r.preamble.id = register_id
    r.preamble.name = name
    r.type_spec.bitstring.bit.bitwidth = bitwidth
    r.size = size
    return p


def test_register_by_name_returns_spec() -> None:
    from p4net.control import RegisterSpec

    idx = P4InfoIndex(_build_p4info_with_register())
    spec = idx.register_by_name("MyIngress.test_register")
    assert isinstance(spec, RegisterSpec)
    assert spec.id == 3001
    assert spec.name == "MyIngress.test_register"
    assert spec.bitwidth == 32
    assert spec.size == 256


def test_register_by_name_unknown_raises() -> None:
    from p4net.control import NoSuchRegisterError

    idx = P4InfoIndex(_build_p4info_with_register())
    with pytest.raises(NoSuchRegisterError, match="no register named 'missing'"):
        idx.register_by_name("missing")


def test_register_names_lists_all() -> None:
    p = _build_p4info_with_register()
    r2 = p.registers.add()
    r2.preamble.id = 3002
    r2.preamble.name = "MyIngress.second_register"
    r2.type_spec.bitstring.bit.bitwidth = 8
    r2.size = 1
    idx = P4InfoIndex(p)
    assert set(idx.register_names) == {"MyIngress.test_register", "MyIngress.second_register"}
