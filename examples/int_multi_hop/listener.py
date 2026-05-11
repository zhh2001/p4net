"""Multi-hop INT listener — decodes a chain of stacked INT shim headers.

Walks the receiving frame's protocol chain starting from the outer
EtherType, parsing one 14-byte shim per hop until ``next_proto`` points
back into a non-INT protocol (typically IPv4, ``0x0800``).

If a coordination file is present at
``/tmp/p4net-int-multi-hop-boot-times.json`` (written by
``topology.py``'s ``setup(net)``), each switch's BMv2 boot timestamp is
loaded and combined with the per-hop ``ingress_timestamp_us`` to print
wall-clock arrival times and a per-hop forwarding-latency line.

Usage (must be run as root for AF_PACKET access):

    sudo ip netns exec h2 python3 listener.py --iface h2-eth0

Or from the p4net interactive shell:

    h2 xterm
    # in the spawned xterm:
    sudo python3 examples/int_multi_hop/listener.py --iface h2-eth0
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
from pathlib import Path

ETH_P_ALL = 0x0003
ETHERTYPE_INT = 0x88B6
ETHERTYPE_IPV4 = 0x0800
SHIM_LEN = 14
DEFAULT_BOOT_TIMES_PATH = Path("/tmp/p4net-int-multi-hop-boot-times.json")
# Map a 1-based hop index in the captured frame to the switch name in the
# coordination file. The 2-switch example always sees s1 first, then s2.
HOP_INDEX_TO_SWITCH = {1: "s1", 2: "s2"}


def _decode_shim(buf: bytes) -> dict[str, int]:
    """Decode one 14-byte INT shim."""
    if len(buf) < SHIM_LEN:
        raise ValueError(f"INT shim truncated: got {len(buf)} bytes, need {SHIM_LEN}")
    return {
        "switch_id": buf[0],
        "ingress_timestamp_us": int.from_bytes(buf[1:7], "big"),
        "egress_port": struct.unpack("!H", buf[7:9])[0],
        "queue_depth": struct.unpack("!H", buf[9:11])[0],
        "next_proto": struct.unpack("!H", buf[11:13])[0],
        "reserved": buf[13],
    }


def _decode_ipv4_addrs(buf: bytes) -> tuple[str, str] | None:
    if len(buf) < 20:
        return None
    src = socket.inet_ntoa(buf[12:16])
    dst = socket.inet_ntoa(buf[16:20])
    return src, dst


def _load_boot_times(path: Path) -> dict[str, int] | None:
    """Return ``{switch_name: boot_timestamp_us}`` or ``None`` if not present."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return {str(k): int(v) for k, v in raw.items()}


def _render_packet(
    hops: list[dict[str, int]],
    next_proto: int,
    flow: str,
    boot_times: dict[str, int] | None,
) -> str:
    """Format one packet's hops for stdout. Used by both modes."""
    lines: list[str] = [f"packet ({len(hops)} hop(s), final proto 0x{next_proto:04x}):{flow}"]
    aligned_per_hop: list[int | None] = []
    for i, hop in enumerate(hops, 1):
        boot_us = None
        if boot_times is not None:
            sw_name = HOP_INDEX_TO_SWITCH.get(i)
            if sw_name is not None:
                boot_us = boot_times.get(sw_name)
        if boot_us is not None:
            aligned_us = boot_us + hop["ingress_timestamp_us"]
            aligned_per_hop.append(aligned_us)
            lines.append(
                f"  hop {i}: switch_id={hop['switch_id']} "
                f"ts={hop['ingress_timestamp_us']}us "
                f"aligned={aligned_us}us "
                f"egress_port={hop['egress_port']} "
                f"queue_depth={hop['queue_depth']}"
            )
        else:
            aligned_per_hop.append(None)
            lines.append(
                f"  hop {i}: switch_id={hop['switch_id']} "
                f"ts={hop['ingress_timestamp_us']}us "
                f"[unaligned] "
                f"egress_port={hop['egress_port']} "
                f"queue_depth={hop['queue_depth']}"
            )
    if boot_times is None:
        lines.append(
            "  (run via `sudo p4net examples/int_multi_hop/topology.py` to get aligned timestamps)"
        )
    elif len(aligned_per_hop) == 2 and all(a is not None for a in aligned_per_hop):
        delta = aligned_per_hop[1] - aligned_per_hop[0]  # type: ignore[operator]
        lines.append(f"  latency_s1_to_s2 = {delta}us")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decode stacked INT shim headers from a raw AF_PACKET socket."
    )
    parser.add_argument(
        "--iface",
        required=True,
        help="Interface name to bind to (e.g. h2-eth0).",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="Exit after printing this many INT frames (0 = forever).",
    )
    parser.add_argument(
        "--boot-times",
        type=Path,
        default=DEFAULT_BOOT_TIMES_PATH,
        help=(
            "Path to the coordination JSON written by topology.py "
            "(default: %(default)s). If missing, timestamps are shown unaligned."
        ),
    )
    args = parser.parse_args()

    boot_times = _load_boot_times(args.boot_times)
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    sock.bind((args.iface, 0))
    if boot_times is not None:
        sys.stdout.write(
            f"[listener] bound on {args.iface}; "
            f"boot times loaded from {args.boot_times}: {boot_times}\n"
        )
    else:
        sys.stdout.write(
            f"[listener] bound on {args.iface}; "
            f"no boot-times file at {args.boot_times} — running unaligned\n"
        )
    sys.stdout.flush()

    seen = 0
    while True:
        frame, _addr = sock.recvfrom(65535)
        if len(frame) < 14 + SHIM_LEN:
            continue
        etype = int.from_bytes(frame[12:14], "big")
        if etype != ETHERTYPE_INT:
            continue

        # Walk the shim chain. ``next_proto`` on each shim points to the
        # next header in order; we stop when it leaves the INT space.
        hops: list[dict[str, int]] = []
        offset = 14
        next_proto = etype
        while next_proto == ETHERTYPE_INT and offset + SHIM_LEN <= len(frame):
            shim = _decode_shim(frame[offset : offset + SHIM_LEN])
            hops.append(shim)
            offset += SHIM_LEN
            next_proto = shim["next_proto"]

        addrs = _decode_ipv4_addrs(frame[offset:]) if next_proto == ETHERTYPE_IPV4 else None
        flow = f" {addrs[0]} -> {addrs[1]}" if addrs else ""
        sys.stdout.write(_render_packet(hops, next_proto, flow, boot_times))
        sys.stdout.flush()
        seen += 1
        if args.count and seen >= args.count:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
