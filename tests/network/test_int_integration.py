"""End-to-end INT (in-band network telemetry) test.

Brings up the `examples/int/topology.py` stack, captures a frame on h2
via raw AF_PACKET in a background thread, sends one ping from h1, and
asserts that the captured frame's INT shim decodes correctly.

Run with:

    sudo -E env "PATH=$PATH" pytest \\
      --run-integration --run-p4c --run-bmv2 \\
      -m "integration and requires_p4c and requires_bmv2" \\
      tests/network/test_int_integration.py
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import uuid
from pathlib import Path

import pytest

from p4net import Network
from p4net.compiler import P4Compiler
from p4net.topo import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_p4c,
    pytest.mark.requires_bmv2,
]

ETHERTYPE_INT = 0x88B6
ETHERTYPE_IPV4 = 0x0800
SHIM_LEN = 14
ETH_HEADER_LEN = 14

_INT_P4 = Path(__file__).resolve().parent.parent.parent / "examples" / "int" / "int.p4"


def _suffix() -> str:
    return uuid.uuid4().hex[:6]


def _two_free_ports() -> tuple[int, int]:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as a,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as b,
    ):
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        return int(a.getsockname()[1]), int(b.getsockname()[1])


@pytest.fixture(scope="session")
def compiled() -> dict[str, Path]:
    cache = Path("/tmp") / f"int-it-cache-{uuid.uuid4().hex[:8]}"
    compiler = P4Compiler(cache_dir=cache)
    result = compiler.compile(_INT_P4)
    return {"bmv2_json": result.bmv2_json, "p4info": result.p4info}


def _capture_int_frame_in_ns(
    ns_name: str,
    iface: str,
    out: dict[str, bytes],
    stop_event: threading.Event,
) -> None:
    """Bind an AF_PACKET socket inside ns ``ns_name`` and capture one INT frame.

    Writes the raw frame to ``out['frame']`` when found.
    """
    import os

    ns_fd = os.open(f"/var/run/netns/{ns_name}", os.O_RDONLY)
    try:
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            CLONE_NEWNET = 0x40000000
            if libc.setns(ns_fd, CLONE_NEWNET) != 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err))
        finally:
            os.close(ns_fd)

        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        sock.bind((iface, 0))
        sock.settimeout(0.5)
        while not stop_event.is_set():
            try:
                frame, _ = sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                continue
            if len(frame) < ETH_HEADER_LEN + SHIM_LEN:
                continue
            etype = int.from_bytes(frame[12:14], "big")
            if etype != ETHERTYPE_INT:
                continue
            out["frame"] = frame
            return
    except Exception as exc:
        out["error"] = repr(exc).encode("utf-8")


def test_int_shim_inserted_on_forwarded_packet(compiled: dict[str, Path], tmp_path: Path) -> None:
    """Single ping h1→h2; capture the frame on h2; decode the 14-byte INT shim."""
    suffix = _suffix()
    h1, h2, s1 = f"h{suffix}a", f"h{suffix}b", f"s{suffix}"
    grpc, thrift = _two_free_ports()
    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    topo.add_switch(s1, p4_src=_INT_P4, grpc_port=grpc, thrift_port=thrift)
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(h2, s1, port_b=2)

    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        h1r, h2r = net.host(h1), net.host(h2)
        h1r.exec(
            [
                "ip",
                "neigh",
                "replace",
                "10.0.0.2",
                "lladdr",
                "00:00:00:00:00:02",
                "dev",
                f"{h1}-eth0",
                "nud",
                "permanent",
            ]
        )
        h2r.exec(
            [
                "ip",
                "neigh",
                "replace",
                "10.0.0.1",
                "lladdr",
                "00:00:00:00:00:01",
                "dev",
                f"{h2}-eth0",
                "nud",
                "permanent",
            ]
        )

        sw = net.switch(s1)
        sw.client.insert_table_entry(
            table="MyIngress.ipv4_lpm",
            match={"hdr.ipv4.dstAddr": "10.0.0.1/32"},
            action="MyIngress.set_egress_port",
            params={"port": 1},
        )
        sw.client.insert_table_entry(
            table="MyIngress.ipv4_lpm",
            match={"hdr.ipv4.dstAddr": "10.0.0.2/32"},
            action="MyIngress.set_egress_port",
            params={"port": 2},
        )
        # New in 1.2: switch_id is register-backed, not const.
        sw.client.write_register("MyIngress.switch_id", index=0, value=1)

        capture: dict[str, bytes] = {}
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_capture_int_frame_in_ns,
            args=(h2, f"{h2}-eth0", capture, stop_event),
        )
        thread.start()
        time.sleep(0.5)  # give the listener a moment to bind

        # Ping from h1 to h2; the switch should insert an INT shim on the way.
        h1r.exec(
            ["ping", "-4", "-c", "3", "-W", "1", "10.0.0.2"],
            capture_output=True,
            check=False,
        )

        # Wait up to 5s for the capture thread to see a matching frame.
        thread.join(timeout=5.0)
        stop_event.set()
        thread.join(timeout=1.0)

        assert "error" not in capture, (
            f"capture thread errored: {capture.get('error', b'').decode()}"
        )
        assert "frame" in capture, "no INT-stamped frame was captured on h2"

        frame = capture["frame"]
        assert len(frame) >= ETH_HEADER_LEN + SHIM_LEN
        assert int.from_bytes(frame[12:14], "big") == ETHERTYPE_INT

        shim = frame[ETH_HEADER_LEN : ETH_HEADER_LEN + SHIM_LEN]
        switch_id = shim[0]
        timestamp_us = int.from_bytes(shim[1:7], "big")
        egress_port, queue_depth, next_proto = struct.unpack("!HHH", shim[7:13])
        reserved = shim[13]

        assert switch_id == 1
        assert timestamp_us > 0
        assert egress_port == 2
        assert queue_depth >= 0  # BMv2 typically reports 0 here
        assert next_proto == ETHERTYPE_IPV4
        assert reserved == 0

        # Stash decoded values where the report can find them.
        decoded = {
            "switch_id": switch_id,
            "ingress_timestamp_us": timestamp_us,
            "egress_port": egress_port,
            "queue_depth": queue_depth,
            "next_proto": next_proto,
            "reserved": reserved,
        }
        (tmp_path / "int_shim_decoded.txt").write_text(repr(decoded))
    finally:
        net.stop()
