"""Interface name validation shared by topology and runtime code."""

from __future__ import annotations

_IFNAME_MAX_BYTES = 15


def _validate_ifname(name: object) -> None:
    if not isinstance(name, str):
        raise ValueError("interface name must be a string")
    if not name:
        raise ValueError("interface name must be non-empty")
    if name in {".", ".."}:
        raise ValueError("interface name must not be '.' or '..'")
    if any(ch.isspace() for ch in name):
        raise ValueError("interface name must not contain whitespace")
    if any(ch in name for ch in ("/", ":", "\0")):
        raise ValueError("interface name must not contain '/', ':' or NUL")
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("interface name must be valid UTF-8") from exc
    if len(encoded) > _IFNAME_MAX_BYTES:
        raise ValueError(f"interface name {name!r} exceeds {_IFNAME_MAX_BYTES} bytes in UTF-8")
