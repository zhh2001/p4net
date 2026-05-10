"""End-to-end CPU-port packet I/O against a running BMv2 + P4Runtime stack.

Run with:

    sudo -E env "PATH=$PATH" pytest \\
      --run-integration --run-p4c --run-bmv2 \\
      -m "integration and requires_p4c and requires_bmv2" \\
      tests/control/test_packet_integration.py

The pipeline punts every dataplane packet to the controller via the CPU
port (510). Controller-injected packets are forwarded according to the
``packet_out`` header's ``egress_port`` metadata.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from p4net import Network
from p4net.cli import CommandDispatcher
from p4net.compiler import P4Compiler
from p4net.topo import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_p4c,
    pytest.mark.requires_bmv2,
]

# Reuse the example's P4 source verbatim — the integration test exercises the
# same pipeline a user would run with `p4net examples/cpu_punt/topology.py`.
_EXAMPLE = Path(__file__).resolve().parent.parent.parent / "examples" / "cpu_punt"
_CPU_PUNT_P4 = _EXAMPLE / "cpu_punt.p4"
_CPU_PORT = 510


def _suffix() -> str:
    return uuid.uuid4().hex[:6]


def _two_free_ports() -> tuple[int, int]:
    """Return two distinct ephemeral ports; the kernel assigns both at once
    so they cannot collide even under same-second back-to-back probes."""
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as a,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as b,
    ):
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        return int(a.getsockname()[1]), int(b.getsockname()[1])


@pytest.fixture(scope="session")
def compiled() -> dict[str, Path]:
    cache = Path("/tmp") / f"cpu-punt-cache-{uuid.uuid4().hex[:8]}"
    compiler = P4Compiler(cache_dir=cache)
    result = compiler.compile(_CPU_PUNT_P4)
    return {"bmv2_json": result.bmv2_json, "p4info": result.p4info}


@pytest.fixture
def network(compiled: dict[str, Path], tmp_path: Path) -> Iterator[Network]:
    """One host plus one switch with cpu_port=510."""
    suffix = _suffix()
    h1, s1 = f"h{suffix}", f"s{suffix}"
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    grpc_port, thrift_port = _two_free_ports()
    topo.add_switch(
        s1,
        p4_src=_CPU_PUNT_P4,
        grpc_port=grpc_port,
        thrift_port=thrift_port,
        cpu_port=_CPU_PORT,
    )
    topo.add_link(h1, s1, port_b=1)
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        net._test_h1 = h1  # type: ignore[attr-defined]
        net._test_s1 = s1  # type: ignore[attr-defined]
        net._test_pcap_dir = tmp_path  # type: ignore[attr-defined]
        yield net
    finally:
        net.stop()


def _names(network: Network) -> tuple[str, str]:
    return (
        network._test_h1,  # type: ignore[attr-defined]
        network._test_s1,  # type: ignore[attr-defined]
    )


def _build_eth_frame(dst_mac: bytes, src_mac: bytes, ethertype: int, payload: bytes) -> bytes:
    """Plain Ethernet frame, no VLAN / CRC."""
    return dst_mac + src_mac + ethertype.to_bytes(2, "big") + payload


def _send_raw_in_host(host_name: str, network: Network, frame: bytes) -> None:
    """Send a raw L2 frame from inside the host's namespace via AF_PACKET."""
    iface = f"{host_name}-eth0"
    script = (
        "import socket\n"
        f"iface = {iface!r}\n"
        f"frame = bytes.fromhex({frame.hex()!r})\n"
        "s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)\n"
        "s.bind((iface, 0))\n"
        "s.send(frame)\n"
    )
    network.host(host_name).exec(["python3", "-c", script], check=True)


# ---------------------------------------------------------------------------
# 1. PacketIn arrives when host generates traffic
# ---------------------------------------------------------------------------


def test_packet_in_from_host(network: Network) -> None:
    h1, s1 = _names(network)
    sw = network.switch(s1)
    import queue as _queue

    q: _queue.Queue[tuple[bytes, dict[str, int]]] = _queue.Queue()
    deregister = sw.client.on_packet_in(lambda p, m: q.put((p, m)))
    try:
        marker = uuid.uuid4().bytes
        payload = b"PUNT" + marker + b"\x00" * 32
        frame = _build_eth_frame(
            dst_mac=b"\xff\xff\xff\xff\xff\xff",
            src_mac=b"\x00\x00\x00\x00\x00\x01",
            ethertype=0x0800,
            payload=payload,
        )
        _send_raw_in_host(h1, network, frame)
        # Filter out IPv6 ND / MLD noise that the host emits on startup.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            p, meta = q.get(timeout=max(deadline - time.monotonic(), 0.05))
            if marker in p:
                assert meta.get("ingress_port") == 1
                return
        raise AssertionError(f"PacketIn with marker {marker!r} not seen within 10s")
    finally:
        deregister()


# ---------------------------------------------------------------------------
# 2. PacketOut delivered to host
# ---------------------------------------------------------------------------


def test_packet_out_to_host(network: Network) -> None:
    h1, s1 = _names(network)
    sw = network.switch(s1)

    pcap_path = network._test_pcap_dir / "cap.pcap"  # type: ignore[attr-defined]
    iface = f"{h1}-eth0"
    # Filter on the local-experimental ethertype so IPv6 ND noise can't
    # consume the `-c 1` slot before the injected frame arrives.
    proc = network.host(h1).popen(
        [
            "tcpdump",
            "-i",
            iface,
            "-c",
            "1",
            "-w",
            str(pcap_path),
            "-Q",
            "in",
            "ether",
            "proto",
            "0x88B5",
        ],
    )
    # Give tcpdump time to attach. tcpdump prints "listening on ..." to
    # stderr; we don't capture stderr to keep the test simple, so a
    # short sleep suffices.
    time.sleep(0.6)

    marker = uuid.uuid4().bytes
    payload = b"INJECT" + marker + b"\x00" * 32
    frame = _build_eth_frame(
        dst_mac=b"\x00\x00\x00\x00\x00\x01",  # h1's MAC
        src_mac=b"\x02\x00\x00\x00\x00\x99",
        ethertype=0x88B5,  # local experimental ethertype
        payload=payload,
    )
    sw.client.send_packet_out(frame, {"egress_port": 1})

    rc = proc.wait(timeout=5.0)
    assert rc == 0
    captured = pcap_path.read_bytes()
    assert marker in captured


# ---------------------------------------------------------------------------
# 3. Multiple PacketIn handlers
# ---------------------------------------------------------------------------


def test_multiple_packet_in_handlers(network: Network) -> None:
    h1, s1 = _names(network)
    sw = network.switch(s1)
    import queue as _queue

    def to_payload_queue(q: _queue.Queue[bytes]) -> Any:
        return lambda p, m: q.put(p)

    q1: _queue.Queue[bytes] = _queue.Queue()
    q2: _queue.Queue[bytes] = _queue.Queue()
    deregister1 = sw.client.on_packet_in(to_payload_queue(q1))
    deregister2 = sw.client.on_packet_in(to_payload_queue(q2))
    try:
        marker = uuid.uuid4().bytes
        frame = _build_eth_frame(
            dst_mac=b"\xff\xff\xff\xff\xff\xff",
            src_mac=b"\x00\x00\x00\x00\x00\x01",
            ethertype=0x0800,
            payload=b"M" + marker + b"\x00" * 32,
        )
        _send_raw_in_host(h1, network, frame)
        _wait_for_marker(q1, marker, timeout=5.0)
        _wait_for_marker(q2, marker, timeout=5.0)
    finally:
        deregister1()
        deregister2()


# ---------------------------------------------------------------------------
# 4. Handler deregister
# ---------------------------------------------------------------------------


def test_handler_deregister_stops_invocation(network: Network) -> None:
    h1, s1 = _names(network)
    sw = network.switch(s1)
    received: list[bytes] = []
    deregister = sw.client.on_packet_in(lambda p, m: received.append(p))
    deregister()
    marker = uuid.uuid4().bytes
    frame = _build_eth_frame(
        dst_mac=b"\xff\xff\xff\xff\xff\xff",
        src_mac=b"\x00\x00\x00\x00\x00\x01",
        ethertype=0x0800,
        payload=b"D" + marker + b"\x00" * 32,
    )
    _send_raw_in_host(h1, network, frame)
    # Give the stream consumer thread a chance to run; it should be a no-op
    # because we deregistered.
    time.sleep(0.5)
    assert all(marker not in p for p in received)


# ---------------------------------------------------------------------------
# 5. Handler exception isolation
# ---------------------------------------------------------------------------


def _wait_for_marker(q: Any, marker: bytes, timeout: float) -> bytes:
    """Pull from queue until a packet containing `marker` arrives, or fail.

    Filters out IPv6 ND / MLD noise that the host emits on startup.
    """
    import queue as _queue

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            p = q.get(timeout=max(remaining, 0.05))
        except _queue.Empty:
            break
        if marker in p:
            return p
    raise AssertionError(f"marker {marker!r} not seen within {timeout}s")


def test_handler_exception_isolated_e2e(network: Network) -> None:
    h1, s1 = _names(network)
    sw = network.switch(s1)
    import queue as _queue

    q: _queue.Queue[bytes] = _queue.Queue()

    def bad(payload: bytes, metadata: dict[str, int]) -> None:
        raise RuntimeError("intentional")

    bad_dereg = sw.client.on_packet_in(bad)
    good_dereg = sw.client.on_packet_in(lambda p, m: q.put(p))
    try:
        marker1 = uuid.uuid4().bytes
        frame1 = _build_eth_frame(
            dst_mac=b"\xff\xff\xff\xff\xff\xff",
            src_mac=b"\x00\x00\x00\x00\x00\x01",
            ethertype=0x0800,
            payload=b"E1" + marker1 + b"\x00" * 32,
        )
        _send_raw_in_host(h1, network, frame1)
        _wait_for_marker(q, marker1, timeout=5.0)
        # Stream thread must still be alive; second send goes through too.
        marker2 = uuid.uuid4().bytes
        frame2 = _build_eth_frame(
            dst_mac=b"\xff\xff\xff\xff\xff\xff",
            src_mac=b"\x00\x00\x00\x00\x00\x01",
            ethertype=0x0800,
            payload=b"E2" + marker2 + b"\x00" * 32,
        )
        _send_raw_in_host(h1, network, frame2)
        _wait_for_marker(q, marker2, timeout=5.0)
    finally:
        bad_dereg()
        good_dereg()


# ---------------------------------------------------------------------------
# 6. Packet round-trip via CLI
# ---------------------------------------------------------------------------


def test_cli_packet_send_then_listen(network: Network) -> None:
    h1, s1 = _names(network)
    d = CommandDispatcher(network, color=False)

    # Send: tcpdump in h1's namespace must see the injected frame. Filter on
    # the local-experimental ethertype so IPv6 ND noise doesn't consume the
    # `-c 1` slot before the controller-injected frame arrives.
    pcap_path = network._test_pcap_dir / "cli_send.pcap"  # type: ignore[attr-defined]
    iface = f"{h1}-eth0"
    proc = network.host(h1).popen(
        [
            "tcpdump",
            "-i",
            iface,
            "-c",
            "1",
            "-w",
            str(pcap_path),
            "-Q",
            "in",
            "ether",
            "proto",
            "0x88B5",
        ],
    )
    time.sleep(0.6)

    marker = uuid.uuid4().bytes
    frame = _build_eth_frame(
        dst_mac=b"\x00\x00\x00\x00\x00\x01",
        src_mac=b"\x02\x00\x00\x00\x00\x99",
        ethertype=0x88B5,
        payload=b"CLI" + marker + b"\x00" * 32,
    )
    out_send = d.dispatch(f"{s1} packet send {frame.hex()} metadata: egress_port=1")
    assert out_send == "ok"
    rc = proc.wait(timeout=5.0)
    assert rc == 0
    assert marker in pcap_path.read_bytes()

    # Listen: while the host emits a frame, the dispatcher's listen output
    # mentions ingress_port=1 and contains our marker. count=20 lets noisy
    # IPv6 ND chatter through alongside the test frame; the marker is the
    # discriminator. Timeout is well above the host's ND chatter window.
    listen_marker = uuid.uuid4().bytes
    listen_frame = _build_eth_frame(
        dst_mac=b"\xff\xff\xff\xff\xff\xff",
        src_mac=b"\x00\x00\x00\x00\x00\x01",
        ethertype=0x88B5,
        payload=b"LS" + listen_marker + b"\x00" * 32,
    )

    def deliver() -> None:
        time.sleep(0.3)
        _send_raw_in_host(h1, network, listen_frame)

    threading.Thread(target=deliver, daemon=True).start()
    out_listen = d.dispatch(f"{s1} packet listen count=20 timeout=3.0")
    assert "[ingress_port=1]" in out_listen
    # Marker hex (32 chars) lives at offset 32 of the payload hex, which is
    # still within the dispatcher's 64-char truncation window.
    assert listen_marker.hex() in out_listen
