"""End-to-end multi-hop INT test.

Brings up the ``examples/int_multi_hop/`` topology, captures a frame on h2
via a raw AF_PACKET socket in a background thread, sends one ping from h1,
and asserts the captured frame carries two stacked INT shims with the
expected metadata from s1 (switch_id=1) and s2 (switch_id=2).

Run with:

    sudo -E env "PATH=$PATH" pytest \\
      --run-integration --run-p4c --run-bmv2 \\
      -m "integration and requires_p4c and requires_bmv2" \\
      tests/network/test_int_multi_hop_integration.py
"""

from __future__ import annotations

import io
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

_EXAMPLE_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "int_multi_hop"
_MULTI_HOP_P4 = _EXAMPLE_DIR / "int_multi_hop.p4"


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
    cache = Path("/tmp") / f"intmh-it-cache-{uuid.uuid4().hex[:8]}"
    compiler = P4Compiler(cache_dir=cache)
    result = compiler.compile(_MULTI_HOP_P4)
    return {"bmv2_json": result.bmv2_json, "p4info": result.p4info}


def _decode_shim(buf: bytes) -> dict[str, int]:
    return {
        "switch_id": buf[0],
        "ingress_timestamp_us": int.from_bytes(buf[1:7], "big"),
        "egress_port": struct.unpack("!H", buf[7:9])[0],
        "queue_depth": struct.unpack("!H", buf[9:11])[0],
        "next_proto": struct.unpack("!H", buf[11:13])[0],
        "reserved": buf[13],
    }


def _capture_int_frame_in_ns(
    ns_name: str,
    iface: str,
    out: dict[str, bytes],
    stop_event: threading.Event,
) -> None:
    """Bind an AF_PACKET socket inside the given netns and capture one INT frame."""
    import ctypes
    import os

    ns_fd = os.open(f"/var/run/netns/{ns_name}", os.O_RDONLY)
    try:
        try:
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
            if len(frame) < ETH_HEADER_LEN + 2 * SHIM_LEN:
                continue
            etype = int.from_bytes(frame[12:14], "big")
            if etype != ETHERTYPE_INT:
                continue
            out["frame"] = frame
            return
    except Exception as exc:
        out["error"] = repr(exc).encode("utf-8")


def _format_listener_output(frame: bytes, boot_times: dict[str, int]) -> str:
    """Render captured frame in the same aligned format ``listener.py`` prints."""
    hop_to_switch = {1: "s1", 2: "s2"}
    offset = ETH_HEADER_LEN
    hops: list[dict[str, int]] = []
    next_proto = int.from_bytes(frame[12:14], "big")
    while next_proto == ETHERTYPE_INT and offset + SHIM_LEN <= len(frame):
        shim = _decode_shim(frame[offset : offset + SHIM_LEN])
        hops.append(shim)
        offset += SHIM_LEN
        next_proto = shim["next_proto"]
    buf = io.StringIO()
    flow = ""
    if next_proto == ETHERTYPE_IPV4 and offset + 20 <= len(frame):
        src = socket.inet_ntoa(frame[offset + 12 : offset + 16])
        dst = socket.inet_ntoa(frame[offset + 16 : offset + 20])
        flow = f" {src} -> {dst}"
    buf.write(f"packet ({len(hops)} hop(s), final proto 0x{next_proto:04x}):{flow}\n")
    aligned: list[int] = []
    for i, hop in enumerate(hops, 1):
        sw_name = hop_to_switch.get(i)
        boot_us = boot_times.get(sw_name) if sw_name else None
        if boot_us is None:
            buf.write(
                f"  hop {i}: switch_id={hop['switch_id']} "
                f"ts={hop['ingress_timestamp_us']}us [unaligned] "
                f"egress_port={hop['egress_port']} "
                f"queue_depth={hop['queue_depth']}\n"
            )
        else:
            aligned_us = boot_us + hop["ingress_timestamp_us"]
            aligned.append(aligned_us)
            buf.write(
                f"  hop {i}: switch_id={hop['switch_id']} "
                f"ts={hop['ingress_timestamp_us']}us "
                f"aligned={aligned_us}us "
                f"egress_port={hop['egress_port']} "
                f"queue_depth={hop['queue_depth']}\n"
            )
    if len(aligned) == 2:
        buf.write(f"  latency_s1_to_s2 = {aligned[1] - aligned[0]}us\n")
    return buf.getvalue()


def test_two_switches_each_insert_their_own_shim(compiled: dict[str, Path], tmp_path: Path) -> None:
    """Single ping h1→h2 traverses s1, s2. Capture on h2; decode 2 stacked shims."""
    suffix = _suffix()
    h1 = f"h{suffix}a"
    h2 = f"h{suffix}b"
    s1 = f"s{suffix}1"
    s2 = f"s{suffix}2"
    grpc1, thrift1 = _two_free_ports()
    grpc2, thrift2 = _two_free_ports()

    topo = Topology()
    topo.add_host(h1, ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    topo.add_host(h2, ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    topo.add_switch(s1, p4_src=_MULTI_HOP_P4, grpc_port=grpc1, thrift_port=thrift1)
    topo.add_switch(s2, p4_src=_MULTI_HOP_P4, grpc_port=grpc2, thrift_port=thrift2)
    topo.add_link(h1, s1, port_b=1)
    topo.add_link(s1, s2, port_a=2, port_b=1)
    topo.add_link(s2, h2, port_a=2)

    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        h1_rt = net.host(h1)
        h2_rt = net.host(h2)
        s1_rt = net.switch(s1)
        s2_rt = net.switch(s2)

        h1_rt.exec(
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
        h2_rt.exec(
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

        s1_rt.client.write_register("MyIngress.switch_id_reg", index=0, value=1)
        s2_rt.client.write_register("MyIngress.switch_id_reg", index=0, value=2)

        # Mirror what examples/int_multi_hop/topology.py setup(net) does: publish
        # each switch's BMv2 boot timestamp so per-switch ``ingress_timestamp_us``
        # values can be aligned to wall clock.
        import json as _json

        boot_times_path = Path("/tmp/p4net-int-multi-hop-boot-times.json")
        boot_times = {
            "s1": s1_rt.boot_timestamp_us,
            "s2": s2_rt.boot_timestamp_us,
        }
        boot_times_path.write_text(_json.dumps(boot_times, indent=2))

        for sw_rt, sw_port in ((s1_rt, 2), (s2_rt, 2)):
            sw_rt.client.insert_table_entry(
                table="MyIngress.l2_forward",
                match={"hdr.ethernet.dstAddr": "00:00:00:00:00:02"},
                action="MyIngress.set_egress_port",
                params={"port": sw_port},
            )
            sw_rt.client.insert_table_entry(
                table="MyIngress.l2_forward",
                match={"hdr.ethernet.dstAddr": "00:00:00:00:00:01"},
                action="MyIngress.set_egress_port",
                params={"port": 1},
            )

        capture: dict[str, bytes] = {}
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_capture_int_frame_in_ns,
            args=(h2, f"{h2}-eth0", capture, stop_event),
        )
        thread.start()
        time.sleep(0.5)

        h1_rt.exec(
            ["ping", "-4", "-c", "3", "-W", "1", "10.0.0.2"],
            capture_output=True,
            check=False,
        )

        thread.join(timeout=5.0)
        stop_event.set()
        thread.join(timeout=1.0)

        assert "error" not in capture, (
            f"capture thread errored: {capture.get('error', b'').decode()}"
        )
        assert "frame" in capture, "no INT-tagged frame captured on h2"

        frame = capture["frame"]
        assert int.from_bytes(frame[12:14], "big") == ETHERTYPE_INT

        shim_1 = _decode_shim(frame[ETH_HEADER_LEN : ETH_HEADER_LEN + SHIM_LEN])
        shim_2 = _decode_shim(frame[ETH_HEADER_LEN + SHIM_LEN : ETH_HEADER_LEN + 2 * SHIM_LEN])

        # Hop 1 is s1 (switch_id=1), egress port 2 toward s2, next_proto = INT.
        assert shim_1["switch_id"] == 1
        assert shim_1["egress_port"] == 2
        assert shim_1["next_proto"] == ETHERTYPE_INT
        assert shim_1["ingress_timestamp_us"] > 0

        # Hop 2 is s2 (switch_id=2), egress port 2 toward h2, next_proto = IPv4.
        assert shim_2["switch_id"] == 2
        assert shim_2["egress_port"] == 2
        assert shim_2["next_proto"] == ETHERTYPE_IPV4
        assert shim_2["ingress_timestamp_us"] > 0

        # v1.4 aligned causal-ordering check. BMv2's ``ingress_global_timestamp``
        # is per-process (zero on each switch's boot); combine with each
        # switch's ``boot_timestamp_us`` to get wall-clock arrival time and
        # compare across hops.
        s1_boot = s1_rt.boot_timestamp_us
        s2_boot = s2_rt.boot_timestamp_us
        assert s1_boot > 1_700_000_000_000_000
        assert s2_boot > 1_700_000_000_000_000
        assert s1_boot != s2_boot

        aligned_s1 = s1_boot + shim_1["ingress_timestamp_us"]
        aligned_s2 = s2_boot + shim_2["ingress_timestamp_us"]
        now_us = time.time_ns() // 1000
        # Both aligned values fall in the current wall-clock window
        # (within 60 s of "now"). A pre-v1.4 raw-timestamp computation
        # would not satisfy this — that's the whole point of alignment.
        assert abs(now_us - aligned_s1) < 60_000_000
        assert abs(now_us - aligned_s2) < 60_000_000
        # 5 ms negative slack accommodates sub-millisecond drift from
        # capturing ``time.time_ns()`` just before Popen vs. BMv2's
        # internal clock zero. In practice the delta is tens to hundreds
        # of microseconds (real per-hop forwarding latency through BMv2's
        # userspace pipeline plus the veth pair).
        assert aligned_s2 >= aligned_s1 - 5000, (
            f"aligned causal order violated: aligned_s1={aligned_s1}us, "
            f"aligned_s2={aligned_s2}us (delta={aligned_s2 - aligned_s1}us)"
        )

        # Coordination file from setup is present and well-formed.
        assert boot_times_path.is_file()
        on_disk = _json.loads(boot_times_path.read_text())
        assert on_disk == {"s1": s1_boot, "s2": s2_boot}

        # Stash a listener-style rendering for the report and README sample.
        rendered = _format_listener_output(frame, boot_times)
        (tmp_path / "int_multi_hop_output.txt").write_text(rendered)
    finally:
        net.stop()
