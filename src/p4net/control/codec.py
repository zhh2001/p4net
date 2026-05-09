"""Value encoding helpers for P4Runtime field matches and action params.

P4Runtime canonical bytestring form (see P4Runtime spec, section 8.4
"Bytestrings"): non-zero values are encoded as the shortest big-endian byte
string starting with a non-zero byte; the value zero is encoded as a single
zero byte. The helpers here return the *full* bitwidth-rounded bytes; the
`canonicalize` helper applies the spec's leading-zero strip when the result
is being placed into an on-the-wire P4Runtime message.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from typing import Any

from p4net.control.exceptions import EncodingError

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def encode_int(value: int, bitwidth: int) -> bytes:
    """Encode an integer as canonical big-endian bytes for the given bitwidth.

    The returned slice is the minimum byte width that holds `bitwidth` bits.
    Raises EncodingError if value is negative or does not fit.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise EncodingError(f"value must be int, got {type(value).__name__}")
    if value < 0:
        raise EncodingError(f"value {value} must be non-negative")
    if bitwidth <= 0:
        raise EncodingError(f"bitwidth must be positive, got {bitwidth}")
    if value.bit_length() > bitwidth:
        raise EncodingError(f"value {value} does not fit in {bitwidth} bits")
    n_bytes = (bitwidth + 7) // 8
    return value.to_bytes(n_bytes, "big")


def encode_ipv4(value: str) -> bytes:
    """Encode '10.0.0.1' as 4 big-endian bytes."""
    if not isinstance(value, str):
        raise EncodingError(f"IPv4 must be a string, got {type(value).__name__}")
    try:
        return ipaddress.IPv4Address(value).packed
    except (ValueError, ipaddress.AddressValueError) as exc:
        raise EncodingError(f"invalid IPv4 address {value!r}") from exc


def encode_mac(value: str) -> bytes:
    """Encode 'aa:bb:cc:dd:ee:ff' as 6 bytes."""
    if not isinstance(value, str) or not _MAC_RE.match(value):
        raise EncodingError(f"invalid MAC address {value!r}")
    return bytes(int(part, 16) for part in value.split(":"))


def encode_value(value: int | str | bytes, bitwidth: int) -> bytes:
    """Auto-dispatch encode for a single field value."""
    if bitwidth <= 0:
        raise EncodingError(f"bitwidth must be positive, got {bitwidth}")
    if isinstance(value, bytes):
        max_bytes = (bitwidth + 7) // 8
        if len(value) > max_bytes:
            raise EncodingError(
                f"bytes value of length {len(value)} exceeds {max_bytes} for bitwidth {bitwidth}"
            )
        return value
    if isinstance(value, bool):
        return encode_int(int(value), bitwidth)
    if isinstance(value, int):
        return encode_int(value, bitwidth)
    if isinstance(value, str):
        if value.count(".") == 3:
            if bitwidth != 32:
                raise EncodingError(f"IPv4 literal {value!r} requires bitwidth=32, got {bitwidth}")
            return encode_ipv4(value)
        if value.count(":") == 5 and _MAC_RE.match(value):
            if bitwidth != 48:
                raise EncodingError(f"MAC literal {value!r} requires bitwidth=48, got {bitwidth}")
            return encode_mac(value)
        try:
            n = int(value, 0)
        except ValueError as exc:
            raise EncodingError(f"cannot parse {value!r} as integer") from exc
        return encode_int(n, bitwidth)
    raise EncodingError(f"unsupported value type {type(value).__name__}")


def decode_int(data: bytes, bitwidth: int) -> int:
    """Inverse of `encode_int`. Accepts canonical or full-width input."""
    if not isinstance(data, bytes):
        raise EncodingError(f"data must be bytes, got {type(data).__name__}")
    if bitwidth <= 0:
        raise EncodingError(f"bitwidth must be positive, got {bitwidth}")
    if len(data) > (bitwidth + 7) // 8:
        raise EncodingError(f"byte string of length {len(data)} too wide for bitwidth {bitwidth}")
    return int.from_bytes(data, "big")


def parse_lpm(
    value: str | tuple[str | int, int],
    bitwidth: int,
) -> tuple[bytes, int]:
    """Accept '10.0.0.0/24' or ('10.0.0.0', 24). Returns (encoded_value, prefix_len)."""
    if isinstance(value, str):
        if "/" not in value:
            raise EncodingError(f"LPM string {value!r} must contain '/'")
        addr_part, prefix_part = value.rsplit("/", 1)
        try:
            prefix_len = int(prefix_part)
        except ValueError as exc:
            raise EncodingError(f"invalid LPM prefix {prefix_part!r}") from exc
        addr: int | str = addr_part
    elif isinstance(value, tuple) and len(value) == 2:
        addr, prefix_len = value
        if not isinstance(prefix_len, int) or isinstance(prefix_len, bool):
            raise EncodingError(f"LPM prefix length must be int, got {type(prefix_len).__name__}")
    else:
        raise EncodingError(f"LPM value must be string or 2-tuple, got {value!r}")
    if prefix_len < 0 or prefix_len > bitwidth:
        raise EncodingError(f"LPM prefix_len {prefix_len} out of range [0, {bitwidth}]")
    encoded = encode_value(addr, bitwidth)
    return encoded, prefix_len


def parse_ternary(
    value: tuple[str | int | bytes, str | int | bytes],
    bitwidth: int,
) -> tuple[bytes, bytes]:
    """Accept ('value', 'mask'). Returns (encoded_value, encoded_mask)."""
    if not (isinstance(value, tuple) and len(value) == 2):
        raise EncodingError(f"ternary value must be a 2-tuple of (value, mask), got {value!r}")
    val_part, mask_part = value
    enc_val = encode_value(val_part, bitwidth)
    enc_mask = encode_value(mask_part, bitwidth)
    return enc_val, enc_mask


def parse_range(
    value: tuple[Any, Any],
    bitwidth: int,
) -> tuple[bytes, bytes]:
    """Accept (low, high). Returns (encoded_low, encoded_high)."""
    if not (isinstance(value, tuple) and len(value) == 2):
        raise EncodingError(f"range value must be a 2-tuple of (low, high), got {value!r}")
    low, high = value
    return encode_value(low, bitwidth), encode_value(high, bitwidth)


def canonicalize(data: bytes) -> bytes:
    """Return P4Runtime canonical form: strip leading zeros; zero -> b'\\x00'."""
    stripped = data.lstrip(b"\x00")
    return stripped if stripped else b"\x00"


def encode_values(values: Sequence[int | str | bytes], bitwidth: int) -> list[bytes]:
    """Convenience: vectorised encode_value for a sequence."""
    return [encode_value(v, bitwidth) for v in values]
