"""Multi-hop INT listener — decodes a chain of stacked INT shim headers.

Walks the receiving frame's protocol chain starting from the outer
EtherType, parsing one 14-byte shim per hop until ``next_proto`` points
back into a non-INT protocol (typically IPv4, ``0x0800``).

Usage (must be run as root for AF_PACKET access):

    sudo ip netns exec h2 python3 listener.py --iface h2-eth0

Or from the p4net interactive shell:

    h2 xterm
    # in the spawned xterm:
    sudo python3 examples/int_multi_hop/listener.py --iface h2-eth0
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys

ETH_P_ALL = 0x0003
ETHERTYPE_INT = 0x88B6
ETHERTYPE_IPV4 = 0x0800
SHIM_LEN = 14


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
    args = parser.parse_args()

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    sock.bind((args.iface, 0))
    sys.stdout.write(f"[listener] bound on {args.iface}, waiting for INT frames\n")
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
        sys.stdout.write(f"packet ({len(hops)} hop(s), final proto 0x{next_proto:04x}):{flow}\n")
        for i, hop in enumerate(hops, 1):
            sys.stdout.write(
                f"  hop {i}: switch_id={hop['switch_id']} "
                f"ts={hop['ingress_timestamp_us']}us "
                f"egress_port={hop['egress_port']} "
                f"queue_depth={hop['queue_depth']}\n"
            )
        sys.stdout.flush()
        seen += 1
        if args.count and seen >= args.count:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
