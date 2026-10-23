"""Unit tests for `p4net.control.codec`."""

from __future__ import annotations

from typing import Any

import pytest

from p4net.control import (
    EncodingError,
    canonicalize,
    decode_int,
    decode_ipv4,
    decode_ipv6,
    decode_mac,
    encode_int,
    encode_ipv4,
    encode_mac,
    encode_value,
    format_exact,
    format_lpm,
    format_range,
    format_ternary,
    parse_lpm,
    parse_range,
    parse_ternary,
)

# ---------------------------------------------------------------------------
# encode_int
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "bitwidth", "expected"),
    [
        (0, 1, b"\x00"),
        (1, 1, b"\x01"),
        (0, 8, b"\x00"),
        (0xFF, 8, b"\xff"),
        (0, 16, b"\x00\x00"),
        (0x1234, 16, b"\x12\x34"),
        (0, 32, b"\x00\x00\x00\x00"),
        (0xDEADBEEF, 32, b"\xde\xad\xbe\xef"),
        (0, 9, b"\x00\x00"),
        (0x1FF, 9, b"\x01\xff"),
    ],
)
def test_encode_int_widths(value: int, bitwidth: int, expected: bytes) -> None:
    assert encode_int(value, bitwidth) == expected


@pytest.mark.parametrize(
    ("value", "bitwidth"),
    [(-1, 8), (256, 8), (1 << 16, 16), (1 << 32, 32)],
)
def test_encode_int_rejects_overflow(value: int, bitwidth: int) -> None:
    with pytest.raises(EncodingError):
        encode_int(value, bitwidth)


@pytest.mark.parametrize("bad_bw", [0, -1, -100])
def test_encode_int_rejects_bad_bitwidth(bad_bw: int) -> None:
    with pytest.raises(EncodingError):
        encode_int(0, bad_bw)


def test_encode_int_rejects_bool() -> None:
    with pytest.raises(EncodingError, match="must be int"):
        encode_int(True, 8)  # type: ignore[arg-type]


def test_encode_int_rejects_non_int() -> None:
    with pytest.raises(EncodingError, match="must be int"):
        encode_int("5", 8)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# encode_ipv4
# ---------------------------------------------------------------------------


def test_encode_ipv4_basic() -> None:
    assert encode_ipv4("0.0.0.0") == b"\x00\x00\x00\x00"
    assert encode_ipv4("10.0.0.1") == b"\x0a\x00\x00\x01"
    assert encode_ipv4("255.255.255.255") == b"\xff\xff\xff\xff"


@pytest.mark.parametrize("bad", ["", "10.0.0", "10.0.0.300", "not.an.ip", "10.0.0.1/24"])
def test_encode_ipv4_invalid(bad: str) -> None:
    with pytest.raises(EncodingError):
        encode_ipv4(bad)


# ---------------------------------------------------------------------------
# encode_mac
# ---------------------------------------------------------------------------


def test_encode_mac_basic() -> None:
    assert encode_mac("aa:bb:cc:dd:ee:ff") == b"\xaa\xbb\xcc\xdd\xee\xff"
    assert encode_mac("00:00:00:00:00:00") == b"\x00" * 6


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "aa:bb:cc:dd:ee",
        "aa-bb-cc-dd-ee-ff",
        "ZZ:ZZ:ZZ:ZZ:ZZ:ZZ",
        "aa:bb:cc:dd:ee:fff",
    ],
)
def test_encode_mac_invalid(bad: str) -> None:
    with pytest.raises(EncodingError):
        encode_mac(bad)


# ---------------------------------------------------------------------------
# encode_value (auto-dispatch)
# ---------------------------------------------------------------------------


def test_encode_value_int() -> None:
    assert encode_value(255, 8) == b"\xff"
    assert encode_value(0, 16) == b"\x00\x00"


def test_encode_value_ipv4_string_requires_bw32() -> None:
    assert encode_value("10.0.0.1", 32) == b"\x0a\x00\x00\x01"
    with pytest.raises(EncodingError, match="bitwidth=32"):
        encode_value("10.0.0.1", 16)


def test_encode_value_mac_string_requires_bw48() -> None:
    assert encode_value("aa:bb:cc:dd:ee:ff", 48) == b"\xaa\xbb\xcc\xdd\xee\xff"
    with pytest.raises(EncodingError, match="bitwidth=48"):
        encode_value("aa:bb:cc:dd:ee:ff", 32)


@pytest.mark.parametrize(
    ("value", "bitwidth", "expected"),
    [
        ("0", 8, b"\x00"),
        ("0xff", 8, b"\xff"),
        ("0b1010", 8, b"\x0a"),
        ("255", 8, b"\xff"),
    ],
)
def test_encode_value_string_int_forms(value: str, bitwidth: int, expected: bytes) -> None:
    assert encode_value(value, bitwidth) == expected


def test_encode_value_unparseable_string() -> None:
    with pytest.raises(EncodingError, match="cannot parse"):
        encode_value("nonsense", 8)


def test_encode_value_bytes_passthrough_with_width_check() -> None:
    assert encode_value(b"\x01\x02", 16) == b"\x01\x02"
    assert encode_value(b"\x01", 16) == b"\x01"
    with pytest.raises(EncodingError, match="exceeds"):
        encode_value(b"\x01\x02\x03", 16)


@pytest.mark.parametrize(
    ("value", "bitwidth"),
    [
        (b"\x02", 1),
        (b"\x80", 7),
        (b"\x02\x00", 9),
        (b"\x80\x00\x00\x00", 31),
        (b"\x02" + b"\x00" * 8, 65),
    ],
)
def test_encode_value_bytes_rejects_unused_high_bits(value: bytes, bitwidth: int) -> None:
    with pytest.raises(EncodingError, match="does not fit"):
        encode_value(value, bitwidth)


@pytest.mark.parametrize("bitwidth", [1, 7, 9, 31, 65])
def test_bytes_codec_accepts_non_byte_aligned_boundaries(bitwidth: int) -> None:
    for value in (0, 1, (1 << bitwidth) - 1):
        full_width = value.to_bytes((bitwidth + 7) // 8, "big")
        for data in (full_width, canonicalize(full_width)):
            assert encode_value(data, bitwidth) == data
            assert decode_int(data, bitwidth) == value


def test_encode_value_bool_treated_as_int() -> None:
    # Bool is a subclass of int in Python, but our encoder explicitly accepts it
    # via the bool branch and forwards as int.
    assert encode_value(True, 8) == b"\x01"
    assert encode_value(False, 8) == b"\x00"


def test_encode_value_unsupported_type() -> None:
    with pytest.raises(EncodingError, match="unsupported value type"):
        encode_value(1.5, 8)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# decode_int
# ---------------------------------------------------------------------------


def test_decode_int_round_trip() -> None:
    for v, bw in [(0, 8), (1, 8), (255, 8), (0x1234, 16), (0xDEADBEEF, 32)]:
        encoded = encode_int(v, bw)
        assert decode_int(encoded, bw) == v


def test_decode_int_accepts_canonical_input() -> None:
    """Canonical (leading-zero-stripped) input also decodes."""
    assert decode_int(b"\x01", 32) == 1
    assert decode_int(b"\x00", 32) == 0


def test_decode_int_rejects_too_wide() -> None:
    with pytest.raises(EncodingError, match="too wide"):
        decode_int(b"\x00\x00\x00\x00\x01", 8)


@pytest.mark.parametrize(
    ("data", "bitwidth"),
    [
        (b"\x02", 1),
        (b"\x80", 7),
        (b"\x02\x00", 9),
        (b"\x80\x00\x00\x00", 31),
        (b"\x02" + b"\x00" * 8, 65),
    ],
)
def test_decode_int_rejects_unused_high_bits(data: bytes, bitwidth: int) -> None:
    with pytest.raises(EncodingError, match="does not fit"):
        decode_int(data, bitwidth)


def test_decode_int_rejects_non_bytes() -> None:
    with pytest.raises(EncodingError, match="must be bytes"):
        decode_int("01", 8)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_lpm
# ---------------------------------------------------------------------------


def test_parse_lpm_string_form() -> None:
    enc, plen = parse_lpm("10.0.0.0/24", 32)
    assert enc == b"\x0a\x00\x00\x00"
    assert plen == 24


def test_parse_lpm_tuple_form() -> None:
    enc, plen = parse_lpm(("10.1.0.0", 16), 32)
    assert enc == b"\x0a\x01\x00\x00"
    assert plen == 16


def test_parse_lpm_string_int_value() -> None:
    enc, plen = parse_lpm((0xC0A80100, 24), 32)
    assert enc == b"\xc0\xa8\x01\x00"
    assert plen == 24


@pytest.mark.parametrize("bad", ["10.0.0.0", "no/slash/here", "10.0.0.0/abc"])
def test_parse_lpm_bad_string(bad: str) -> None:
    with pytest.raises(EncodingError):
        parse_lpm(bad, 32)


@pytest.mark.parametrize("plen", [-1, 33, 100])
def test_parse_lpm_bad_prefix_len(plen: int) -> None:
    with pytest.raises(EncodingError, match="out of range"):
        parse_lpm(("10.0.0.0", plen), 32)


def test_parse_lpm_rejects_non_int_prefix() -> None:
    with pytest.raises(EncodingError, match="prefix length must be int"):
        parse_lpm(("10.0.0.0", "24"), 32)  # type: ignore[arg-type]


def test_parse_lpm_rejects_unknown_form() -> None:
    with pytest.raises(EncodingError, match="must be string or 2-tuple"):
        parse_lpm(123, 32)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "bitwidth"),
    [
        ("10.0.0.5/24", 32),
        (("10.0.0.5", 24), 32),
        ("fd00::1/64", 128),
        (("fd00::1", 64), 128),
        (("00:00:00:00:00:01", 40), 48),
        ((0x101, 1), 9),
        ((0x101, 4), 9),
        ((0x1FF, 8), 9),
        ((1, 1), 2),
        ((b"\x01\x01", 4), 9),
        (("0x101", 4), 9),
        (("2001:db8:0:0:8000::1", 65), 128),
        (("0x1", 1), 128),
    ],
)
def test_parse_lpm_rejects_nonzero_host_bits(value: Any, bitwidth: int) -> None:
    with pytest.raises(EncodingError, match="outside the prefix"):
        parse_lpm(value, bitwidth)


@pytest.mark.parametrize(
    ("value", "bitwidth", "expected", "length"),
    [
        ("10.0.0.5/32", 32, 0x0A000005, 32),
        ("fd00::1/128", 128, (0xFD00 << 112) | 1, 128),
        (("fd00:0:0:0:8000::", 65), 128, (0xFD00 << 112) | (1 << 63), 65),
        ("01:02:03:04:05:06/48", 48, 0x010203040506, 48),
        ("00:01:02:03:04:00/40", 48, 0x0102030400, 40),
        ((0x100, 1), 9, 0x100, 1),
        ((0x1E0, 4), 9, 0x1E0, 4),
        ((0x1FE, 8), 9, 0x1FE, 8),
        ((0x1FF, 9), 9, 0x1FF, 9),
        ((2, 1), 2, 2, 1),
        ((b"\x01\xe0", 4), 9, 0x1E0, 4),
        ("0x1e0/4", 9, 0x1E0, 4),
        ((b"\x80", 2), 9, 0x80, 2),
    ],
)
def test_parse_lpm_accepts_aligned_and_full_width_prefixes(
    value: Any, bitwidth: int, expected: int, length: int
) -> None:
    encoded, prefix = parse_lpm(value, bitwidth)
    assert prefix == length
    assert int.from_bytes(encoded, "big") == expected


@pytest.mark.parametrize(
    ("value", "bitwidth", "expected"),
    [
        ("10.0.0.5/0", 32, 0x0A000005),
        ("fd00::dead/0", 128, (0xFD00 << 112) | 0xDEAD),
        (("aa:bb:cc:dd:ee:ff", 0), 48, 0xAABBCCDDEEFF),
        ((511, 0), 9, 511),
        ((b"\x01\xff", 0), 9, 511),
    ],
)
def test_parse_lpm_zero_prefix_preserves_validated_value(
    value: Any, bitwidth: int, expected: int
) -> None:
    encoded, prefix = parse_lpm(value, bitwidth)
    assert prefix == 0
    assert int.from_bytes(encoded, "big") == expected


@pytest.mark.parametrize(
    ("value", "bitwidth"),
    [("bad/0", 32), ((512, 0), 9), ((b"\x02\x00", 0), 9), ("2001:db8::1/0", 32)],
)
def test_parse_lpm_zero_prefix_still_validates_value(value: Any, bitwidth: int) -> None:
    with pytest.raises(EncodingError):
        parse_lpm(value, bitwidth)


# ---------------------------------------------------------------------------
# parse_ternary
# ---------------------------------------------------------------------------


def test_parse_ternary_basic() -> None:
    v, m = parse_ternary((0xAB, 0xFF), 8)
    assert v == b"\xab"
    assert m == b"\xff"


def test_parse_ternary_mixed_str_int() -> None:
    v, m = parse_ternary(("0x10", 0xF0), 8)
    assert v == b"\x10"
    assert m == b"\xf0"


def test_parse_ternary_rejects_non_tuple() -> None:
    with pytest.raises(EncodingError, match="2-tuple"):
        parse_ternary("not a tuple", 8)  # type: ignore[arg-type]


def test_parse_ternary_widths_match() -> None:
    v, m = parse_ternary((0x12345678, 0xFFFFFFFF), 32)
    assert len(v) == 4
    assert len(m) == 4


# ---------------------------------------------------------------------------
# parse_range
# ---------------------------------------------------------------------------


def test_parse_range_basic() -> None:
    low, high = parse_range((10, 20), 16)
    assert low == b"\x00\x0a"
    assert high == b"\x00\x14"


def test_parse_range_rejects_non_tuple() -> None:
    with pytest.raises(EncodingError, match="2-tuple"):
        parse_range([10, 20], 8)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "bitwidth"),
    [
        ((1, 0), 1),
        ((256, 255), 9),
        ((65535, 0), 16),
        (((1 << 128) - 1, 0), 128),
        (("100", "20"), 16),
        (("0x100", "0xff"), 16),
        ((b"\x01\x00", b"\xff"), 16),
        ((b"\x00\x02", b"\x01"), 16),
        ((256, "0xff"), 16),
        (("10.0.1.0", "10.0.0.255"), 32),
        (("00:00:00:00:01:00", "00:00:00:00:00:ff"), 48),
        (("::100", "::ff"), 128),
    ],
)
def test_parse_range_rejects_reversed_bounds(value: tuple[object, object], bitwidth: int) -> None:
    with pytest.raises(EncodingError, match=r"lower bound.*upper bound"):
        parse_range(value, bitwidth)


@pytest.mark.parametrize(
    ("value", "bitwidth", "expected"),
    [
        ((0, 0), 1, (0, 0)),
        ((511, 511), 9, (511, 511)),
        (((1 << 128) - 1, (1 << 128) - 1), 128, ((1 << 128) - 1, (1 << 128) - 1)),
        ((0, 1), 1, (0, 1)),
        ((255, 256), 9, (255, 256)),
        (("20", "100"), 16, (20, 100)),
        (("0xff", "0x100"), 16, (255, 256)),
        ((b"\xff", b"\x01\x00"), 16, (255, 256)),
        ((b"\x01", b"\x00\x01"), 16, (1, 1)),
        (("255", 256), 16, (255, 256)),
        (("10.0.0.255", "10.0.1.0"), 32, (0x0A0000FF, 0x0A000100)),
        (("00:00:00:00:00:ff", "00:00:00:00:01:00"), 48, (255, 256)),
        (("::ff", "::100"), 128, (255, 256)),
    ],
)
def test_parse_range_compares_numeric_bounds(
    value: tuple[object, object], bitwidth: int, expected: tuple[int, int]
) -> None:
    encoded = parse_range(value, bitwidth)
    assert tuple(int.from_bytes(bound, "big") for bound in encoded) == expected


def test_parse_range_preserves_encoded_byte_widths() -> None:
    assert parse_range((b"\x01", b"\x00\x02"), 16) == (b"\x01", b"\x00\x02")


# ---------------------------------------------------------------------------
# canonicalize
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\x00", b"\x00"),
        (b"\x00\x00\x00", b"\x00"),
        (b"\x01", b"\x01"),
        (b"\x00\x00\x01", b"\x01"),
        (b"\x00\x01\x00", b"\x01\x00"),
        (b"\xff\xff", b"\xff\xff"),
    ],
)
def test_canonicalize(data: bytes, expected: bytes) -> None:
    assert canonicalize(data) == expected


# ---------------------------------------------------------------------------
# decode_ipv4 / decode_mac
# ---------------------------------------------------------------------------


def test_decode_ipv4_full_width() -> None:
    assert decode_ipv4(b"\x0a\x00\x00\x05") == "10.0.0.5"
    assert decode_ipv4(b"\xff\xff\xff\xff") == "255.255.255.255"
    assert decode_ipv4(b"\x00\x00\x00\x00") == "0.0.0.0"


def test_decode_ipv4_canonical_input() -> None:
    # canonical = leading zeros stripped; high-side padding restores the address.
    # b'\x0a' (canonical of 0.0.0.10) zero-extends to b'\x00\x00\x00\x0a'.
    assert decode_ipv4(b"\x0a") == "0.0.0.10"
    assert decode_ipv4(b"\x00") == "0.0.0.0"
    assert decode_ipv4(b"") == "0.0.0.0"


def test_decode_ipv4_round_trip() -> None:
    for ip in ["0.0.0.0", "10.0.0.5", "192.168.1.1", "255.255.255.255"]:
        assert decode_ipv4(encode_ipv4(ip)) == ip


def test_decode_ipv4_too_wide() -> None:
    with pytest.raises(EncodingError, match="too wide"):
        decode_ipv4(b"\x00\x00\x00\x00\x00")


def test_decode_ipv4_rejects_non_bytes() -> None:
    with pytest.raises(EncodingError, match="must be bytes"):
        decode_ipv4("10.0.0.5")  # type: ignore[arg-type]


def test_decode_mac_full_width() -> None:
    assert decode_mac(b"\xaa\xbb\xcc\xdd\xee\xff") == "aa:bb:cc:dd:ee:ff"
    assert decode_mac(b"\x00" * 6) == "00:00:00:00:00:00"


def test_decode_mac_canonical_input() -> None:
    # canonical of 00:00:00:00:00:01 is b'\x01'
    assert decode_mac(b"\x01") == "00:00:00:00:00:01"


def test_decode_mac_round_trip() -> None:
    for mac in ["00:00:00:00:00:00", "aa:bb:cc:dd:ee:ff", "00:00:00:00:00:01"]:
        assert decode_mac(encode_mac(mac)) == mac


# ---------------------------------------------------------------------------
# format_lpm / format_ternary / format_range / format_exact
# ---------------------------------------------------------------------------


def test_format_lpm_full_width() -> None:
    assert format_lpm(b"\x0a\x00\x00\x00", 24, 32) == "10.0.0.0/24"


def test_format_lpm_canonical_value() -> None:
    # P4Runtime canonical strips leading zero bytes from the most-significant
    # side; b"\n" therefore decodes as the integer 10 (IPv4 0.0.0.10), not
    # 10.0.0.0. format_lpm honours that by high-side zero-extending.
    assert format_lpm(b"\n", 8, 32) == "0.0.0.10/8"


def test_format_lpm_int_field() -> None:
    # 16-bit field, value 5, prefix 12 -> decimal
    assert format_lpm(b"\x00\x05", 12, 16) == "5/12"


def test_format_ternary_ipv4() -> None:
    assert format_ternary(b"\x0a\x00\x00\x00", b"\xff\xff\x00\x00", 32) == "10.0.0.0&255.255.0.0"


def test_format_ternary_int() -> None:
    assert format_ternary(b"\xab", b"\xff", 8) == "171&255"


def test_format_range_int() -> None:
    assert format_range(b"\x04\x00", b"\xff\xff", 16) == "[1024,65535]"


def test_format_exact_ipv4() -> None:
    assert format_exact(b"\x0a\x00\x00\x01", 32) == "10.0.0.1"


def test_format_exact_mac() -> None:
    assert format_exact(b"\x00\x00\x00\x00\x00\x01", 48) == "00:00:00:00:00:01"


def test_format_exact_int() -> None:
    assert format_exact(b"\x01\xff", 9) == "511"


# ---------------------------------------------------------------------------
# decode_ipv6
# ---------------------------------------------------------------------------


def test_decode_ipv6_full_width() -> None:
    full = b"\xfd\x00" + b"\x00" * 13 + b"\x01"
    assert len(full) == 16
    assert decode_ipv6(full) == "fd00::1"


def test_decode_ipv6_canonical_short_input_zero_extends_high_side() -> None:
    # Spec says high-side zero-extend: b"\xfd\x00" canonicalises something
    # whose value is 0xfd00 — IPv6 0::fd00.
    assert decode_ipv6(b"\xfd\x00") == "::fd00"
    # Single non-zero byte: 0::ff
    assert decode_ipv6(b"\xff") == "::ff"
    # Empty / all-zero canonical -> all-zero address.
    assert decode_ipv6(b"") == "::"
    assert decode_ipv6(b"\x00") == "::"


def test_decode_ipv6_round_trip_through_encode_value() -> None:
    for addr in ("::", "::1", "fd00::1", "2001:db8::1"):
        encoded = encode_value(addr, 128)
        assert decode_ipv6(encoded) == addr


def test_decode_ipv6_too_wide() -> None:
    with pytest.raises(EncodingError, match="too wide"):
        decode_ipv6(b"\x00" * 17)


def test_decode_ipv6_rejects_non_bytes() -> None:
    with pytest.raises(EncodingError, match="must be bytes"):
        decode_ipv6("fd00::1")  # type: ignore[arg-type]


def test_format_lpm_ipv6() -> None:
    full = b"\xfd\x00" + b"\x00" * 14
    assert format_lpm(full, 64, 128) == "fd00::/64"


def test_format_ternary_ipv6() -> None:
    val = b"\xfd\x00" + b"\x00" * 14
    mask = b"\xff\xff" + b"\x00" * 14
    assert format_ternary(val, mask, 128) == "fd00::&ffff::"


def test_format_range_ipv6() -> None:
    lo = b"\xfd\x00" + b"\x00" * 14
    hi = b"\xfd\x00" + b"\x00" * 13 + b"\xff"
    assert format_range(lo, hi, 128) == "[fd00::,fd00::ff]"


def test_format_exact_ipv6() -> None:
    full = b"\xfd\x00" + b"\x00" * 13 + b"\x01"
    assert format_exact(full, 128) == "fd00::1"
