"""INT shim listener — runs inside a host namespace, prints per-frame INT data.

Usage (must be run as root because AF_PACKET sockets are privileged):

    sudo ip netns exec h2 python3 listener.py --iface h2-eth0

Or from the p4net interactive shell:

    h2 xterm
    # in the spawned xterm:
    sudo python3 examples/int/listener.py --iface h2-eth0

The script opens a raw AF_PACKET socket, filters by EtherType 0x88B6 (the
INT shim), and decodes the 14-byte shim that follows the Ethernet header.

Wire layout (matches the deparser in int.p4):

    [ Ethernet (14 B, etherType = 0x88B6) ]
    [ INT shim (14 B):                    ]
        switch_id            uint8
        ingress_timestamp_us uint48 (big-endian, packed in 6 bytes; BMv2 reports microseconds)
        egress_port          uint16
        queue_depth          uint16
        next_proto           uint16 (= 0x0800 for IPv4)
        reserved             uint8
    [ IPv4 + payload                       ]
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys

ETH_P_ALL = 0x0003
ETHERTYPE_INT = 0x88B6
SHIM_LEN = 14


def _decode_int_shim(buf: bytes) -> dict[str, int]:
    """Decode a 14-byte INT shim into a dict."""
    if len(buf) < SHIM_LEN:
        raise ValueError(f"INT shim truncated: got {len(buf)} bytes, need {SHIM_LEN}")
    switch_id = buf[0]
    # 48-bit big-endian timestamp packed in 6 bytes.
    ts = int.from_bytes(buf[1:7], "big")
    egress_port, queue_depth, next_proto = struct.unpack("!HHH", buf[7:13])
    reserved = buf[13]
    return {
        "switch_id": switch_id,
        "ingress_timestamp_us": ts,
        "egress_port": egress_port,
        "queue_depth": queue_depth,
        "next_proto": next_proto,
        "reserved": reserved,
    }


def _decode_ipv4_addrs(buf: bytes) -> tuple[str, str] | None:
    """Pull src/dst from a buffer beginning at the IPv4 header. Returns None on truncation."""
    if len(buf) < 20:
        return None
    src = socket.inet_ntoa(buf[12:16])
    dst = socket.inet_ntoa(buf[16:20])
    return src, dst


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decode INT shim headers from a raw AF_PACKET socket."
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
        shim = _decode_int_shim(frame[14 : 14 + SHIM_LEN])
        inner = frame[14 + SHIM_LEN :]
        addrs = _decode_ipv4_addrs(inner) if shim["next_proto"] == 0x0800 else None
        flow = f" {addrs[0]} -> {addrs[1]}" if addrs else ""
        sys.stdout.write(
            f"[switch={shim['switch_id']} "
            f"ts={shim['ingress_timestamp_us']}us "
            f"egress={shim['egress_port']} "
            f"queue={shim['queue_depth']} "
            f"next_proto=0x{shim['next_proto']:04x}]{flow}\n"
        )
        sys.stdout.flush()
        seen += 1
        if args.count and seen >= args.count:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
