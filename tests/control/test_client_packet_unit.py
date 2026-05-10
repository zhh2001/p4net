"""Unit tests for `P4RuntimeClient` controller packet I/O.

All gRPC plumbing is mocked: tests poke ``_outgoing`` to inspect packet_out
sends, and call ``_dispatch_packet_in`` directly to simulate a synthetic
``StreamMessageResponse(packet=...)`` arriving from the consumer thread.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2

import p4net.control  # noqa: F401  protobuf python-impl env var
from p4net.control import P4InfoIndex, P4RuntimeClient
from p4net.control.exceptions import ConnectionError as P4ConnectionError
from p4net.control.exceptions import EncodingError, NoSuchFieldError, P4RuntimeError


def _build_p4info_with_controller_meta() -> p4info_pb2.P4Info:
    p = p4info_pb2.P4Info()
    cpm_in = p.controller_packet_metadata.add()
    cpm_in.preamble.id = 80000001
    cpm_in.preamble.name = "packet_in"
    m = cpm_in.metadata.add()
    m.id = 1
    m.name = "ingress_port"
    m.bitwidth = 9
    m = cpm_in.metadata.add()
    m.id = 2
    m.name = "_pad0"
    m.bitwidth = 7
    cpm_out = p.controller_packet_metadata.add()
    cpm_out.preamble.id = 80000002
    cpm_out.preamble.name = "packet_out"
    m = cpm_out.metadata.add()
    m.id = 1
    m.name = "egress_port"
    m.bitwidth = 9
    m = cpm_out.metadata.add()
    m.id = 2
    m.name = "_pad0"
    m.bitwidth = 7
    return p


def _connected_client() -> P4RuntimeClient:
    """Build a client with the connected/index state set up by hand."""
    c = P4RuntimeClient("127.0.0.1:50051", 0)
    c._connected = True  # type: ignore[attr-defined]
    c._closed = False  # type: ignore[attr-defined]
    c._index = P4InfoIndex(_build_p4info_with_controller_meta())  # type: ignore[attr-defined]
    c._outgoing = queue.Queue()  # type: ignore[attr-defined]
    return c


def _wrap_packet(payload: bytes, metadata_pairs: list[tuple[int, bytes]]) -> Any:
    pkt = p4runtime_pb2.PacketIn()
    pkt.payload = payload
    for mid, val in metadata_pairs:
        pm = pkt.metadata.add()
        pm.metadata_id = mid
        pm.value = val
    return pkt


# ---------------------------------------------------------------------------
# send_packet_out
# ---------------------------------------------------------------------------


def test_send_packet_out_basic() -> None:
    c = _connected_client()
    c.send_packet_out(b"hello", {"egress_port": 1})
    assert c._outgoing is not None  # type: ignore[attr-defined]
    req = c._outgoing.get_nowait()  # type: ignore[attr-defined]
    assert req.packet.payload == b"hello"
    by_id = {pm.metadata_id: pm for pm in req.packet.metadata}
    assert by_id[1].value == b"\x01"
    assert by_id[2].value == b"\x00"


def test_send_packet_out_no_metadata_zero_pads() -> None:
    c = _connected_client()
    c.send_packet_out(b"", {})
    req = c._outgoing.get_nowait()  # type: ignore[attr-defined]
    assert req.packet.payload == b""
    # Both metadata fields are auto-zero-padded.
    assert len(req.packet.metadata) == 2


def test_send_packet_out_unknown_field_raises() -> None:
    c = _connected_client()
    with pytest.raises(NoSuchFieldError):
        c.send_packet_out(b"x", {"bogus": 1})


def test_send_packet_out_when_disconnected_raises() -> None:
    c = P4RuntimeClient("127.0.0.1:50051", 0)
    with pytest.raises(P4ConnectionError):
        c.send_packet_out(b"x", {"egress_port": 1})


def test_send_packet_out_rejects_non_bytes_payload() -> None:
    c = _connected_client()
    with pytest.raises(EncodingError):
        c.send_packet_out("hello", {"egress_port": 1})  # type: ignore[arg-type]


def test_send_packet_out_uses_index_encode(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _connected_client()
    spy = MagicMock(wraps=c._index.encode_packet_out_metadata)  # type: ignore[union-attr]
    monkeypatch.setattr(c._index, "encode_packet_out_metadata", spy)  # type: ignore[union-attr]
    c.send_packet_out(b"abc", {"egress_port": 2})
    spy.assert_called_once()


# ---------------------------------------------------------------------------
# on_packet_in / dispatch
# ---------------------------------------------------------------------------


def test_on_packet_in_invokes_handler() -> None:
    c = _connected_client()
    received: list[tuple[bytes, dict[str, int]]] = []
    c.on_packet_in(lambda payload, meta: received.append((payload, meta)))
    c._dispatch_packet_in(_wrap_packet(b"hi", [(1, b"\x05"), (2, b"\x00")]))  # type: ignore[attr-defined]
    assert received == [(b"hi", {"ingress_port": 5, "_pad0": 0})]


def test_multiple_handlers_invoked_in_order() -> None:
    c = _connected_client()
    seen: list[str] = []
    c.on_packet_in(lambda p, m: seen.append("a"))
    c.on_packet_in(lambda p, m: seen.append("b"))
    c.on_packet_in(lambda p, m: seen.append("c"))
    c._dispatch_packet_in(_wrap_packet(b"x", [(1, b"\x01")]))  # type: ignore[attr-defined]
    assert seen == ["a", "b", "c"]


def test_handler_exception_isolated() -> None:
    c = _connected_client()
    seen: list[str] = []

    def bad(payload: bytes, meta: dict[str, int]) -> None:
        raise RuntimeError("nope")

    c.on_packet_in(bad)
    c.on_packet_in(lambda p, m: seen.append("ok"))
    c._dispatch_packet_in(_wrap_packet(b"x", [(1, b"\x01")]))  # type: ignore[attr-defined]
    assert seen == ["ok"]


def test_deregister_stops_invocation() -> None:
    c = _connected_client()
    seen: list[bytes] = []
    deregister = c.on_packet_in(lambda p, m: seen.append(p))
    c._dispatch_packet_in(_wrap_packet(b"first", [(1, b"\x01")]))  # type: ignore[attr-defined]
    deregister()
    c._dispatch_packet_in(_wrap_packet(b"second", [(1, b"\x01")]))  # type: ignore[attr-defined]
    assert seen == [b"first"]


def test_double_deregister_is_silent() -> None:
    c = _connected_client()
    deregister = c.on_packet_in(lambda p, m: None)
    deregister()
    deregister()  # must not raise


# ---------------------------------------------------------------------------
# expect_packet_in
# ---------------------------------------------------------------------------


def test_expect_packet_in_returns_when_ready() -> None:
    c = _connected_client()

    # Use a thread to simulate the consumer dispatching after a small delay.
    def deliver() -> None:
        time.sleep(0.05)
        c._dispatch_packet_in(_wrap_packet(b"ping", [(1, b"\x07")]))  # type: ignore[attr-defined]

    t = threading.Thread(target=deliver, daemon=True)
    t.start()
    payload, meta = c.expect_packet_in(timeout=2.0)
    t.join()
    assert payload == b"ping"
    # Only the supplied metadata pair gets decoded; missing ids stay absent.
    assert meta == {"ingress_port": 7}


def test_expect_packet_in_times_out() -> None:
    c = _connected_client()
    with pytest.raises(P4RuntimeError, match="no PacketIn"):
        c.expect_packet_in(timeout=0.05)


def test_expect_packet_in_deregisters_handler() -> None:
    c = _connected_client()
    with pytest.raises(P4RuntimeError):
        c.expect_packet_in(timeout=0.05)
    # No leaked handler.
    assert c._packet_in_handlers == []  # type: ignore[attr-defined]
