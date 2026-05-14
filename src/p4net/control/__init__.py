"""P4Runtime control plane: gRPC client, P4Info index, value codecs.

This package depends on the `p4runtime` PyPI distribution for the generated
proto stubs. Those stubs are descriptor-incompatible with the C++
`google.protobuf` runtime shipped in protobuf 4.x and later, so we set
``PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python`` here, before any p4 stubs
are imported transitively. This is the upstream-recommended workaround
until the `p4runtime` distribution is rebuilt against modern protoc.
"""

from __future__ import annotations

import os as _os

_os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

# `client` imports the p4 stubs at module load; ensure that happens after the
# protobuf python-impl env var is set above.
from p4net.control.async_client import AsyncP4RuntimeClient
from p4net.control.client import CounterData, P4RuntimeClient
from p4net.control.codec import (
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
from p4net.control.exceptions import (
    AsyncOperationCancelledError,
    ConnectionError,
    DuplicateEntryError,
    EncodingError,
    EntryNotFoundError,
    NoSuchActionError,
    NoSuchFieldError,
    NoSuchRegisterError,
    NoSuchTableError,
    NotPrimaryError,
    P4RuntimeError,
    PipelineError,
)
from p4net.control.p4info_index import P4InfoIndex, RegisterSpec

__all__ = [
    "AsyncOperationCancelledError",
    "AsyncP4RuntimeClient",
    "ConnectionError",
    "CounterData",
    "DuplicateEntryError",
    "EncodingError",
    "EntryNotFoundError",
    "NoSuchActionError",
    "NoSuchFieldError",
    "NoSuchRegisterError",
    "NoSuchTableError",
    "NotPrimaryError",
    "P4InfoIndex",
    "P4RuntimeClient",
    "P4RuntimeError",
    "PipelineError",
    "RegisterSpec",
    "canonicalize",
    "decode_int",
    "decode_ipv4",
    "decode_ipv6",
    "decode_mac",
    "encode_int",
    "encode_ipv4",
    "encode_mac",
    "encode_value",
    "format_exact",
    "format_lpm",
    "format_range",
    "format_ternary",
    "parse_lpm",
    "parse_range",
    "parse_ternary",
]
