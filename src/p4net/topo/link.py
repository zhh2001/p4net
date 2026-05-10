"""`Link` and `LinkEndpoint` descriptors for a single veth pair in a topology."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from p4net.topo.exceptions import TopologyError

_MTU_MIN = 68
_MTU_MAX = 65535
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


@dataclass(frozen=True)
class LinkEndpoint:
    """One end of a `Link`, attached to a host or switch.

    ``ip`` and ``ip6`` are link-level overrides that apply only on host
    endpoints; switch endpoints don't carry L3 addresses.
    """

    node: str
    port: int | None = None
    iface_name: str | None = None
    ip: str | None = None
    mac: str | None = None
    ip6: str | None = None

    def __post_init__(self) -> None:
        if self.mac is not None and not _MAC_RE.match(self.mac):
            raise TopologyError(
                f"invalid LinkEndpoint MAC {self.mac!r}: must be 'XX:XX:XX:XX:XX:XX'"
            )
        if self.ip6 is not None:
            if isinstance(self.ip6, str) and "." in self.ip6:
                raise TopologyError(f"invalid LinkEndpoint ip6 {self.ip6!r}: must be an IPv6 CIDR")
            try:
                ipaddress.IPv6Interface(self.ip6)
            except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError) as exc:
                raise TopologyError(
                    f"invalid LinkEndpoint ip6 {self.ip6!r}: must be an IPv6 CIDR"
                ) from exc


@dataclass(frozen=True)
class Link:
    """A bidirectional veth pair between two nodes, with optional impairments.

    Symmetric impairments (``bandwidth`` / ``delay`` / ``jitter`` /
    ``loss_pct``) apply equally to both veth sides. Per-direction overrides
    (``*_a_to_b`` / ``*_b_to_a``) shape only one direction; mixing a symmetric
    field and a matching asymmetric field for the same parameter is rejected.
    """

    a: LinkEndpoint
    b: LinkEndpoint
    bandwidth: str | None = None
    delay: str | None = None
    jitter: str | None = None
    loss_pct: float | None = None
    mtu: int | None = None
    bandwidth_a_to_b: str | None = None
    bandwidth_b_to_a: str | None = None
    delay_a_to_b: str | None = None
    delay_b_to_a: str | None = None
    jitter_a_to_b: str | None = None
    jitter_b_to_a: str | None = None
    loss_pct_a_to_b: float | None = None
    loss_pct_b_to_a: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.a, LinkEndpoint):
            raise TopologyError("Link.a must be a LinkEndpoint")
        if not isinstance(self.b, LinkEndpoint):
            raise TopologyError("Link.b must be a LinkEndpoint")
        if not self.a.node:
            raise TopologyError("Link.a.node must be a non-empty string")
        if not self.b.node:
            raise TopologyError("Link.b.node must be a non-empty string")
        for param, sym, asym in (
            ("bandwidth", self.bandwidth, (self.bandwidth_a_to_b, self.bandwidth_b_to_a)),
            ("delay", self.delay, (self.delay_a_to_b, self.delay_b_to_a)),
            ("jitter", self.jitter, (self.jitter_a_to_b, self.jitter_b_to_a)),
            ("loss_pct", self.loss_pct, (self.loss_pct_a_to_b, self.loss_pct_b_to_a)),
        ):
            if sym is not None and any(a is not None for a in asym):
                raise TopologyError(f"link sets both {param} and {param}_<dir>; pick one")
        if self.jitter is not None and self.delay is None:
            raise TopologyError("link jitter requires delay to be set")
        if self.jitter_a_to_b is not None and self.delay_a_to_b is None and self.delay is None:
            raise TopologyError("link jitter_a_to_b requires delay_a_to_b or delay to be set")
        if self.jitter_b_to_a is not None and self.delay_b_to_a is None and self.delay is None:
            raise TopologyError("link jitter_b_to_a requires delay_b_to_a or delay to be set")
        if self.loss_pct is not None and not 0.0 <= self.loss_pct <= 100.0:
            raise TopologyError(f"link loss_pct {self.loss_pct} out of range [0.0, 100.0]")
        for direction, value in (
            ("a_to_b", self.loss_pct_a_to_b),
            ("b_to_a", self.loss_pct_b_to_a),
        ):
            if value is not None and not 0.0 <= value <= 100.0:
                raise TopologyError(f"link loss_pct_{direction} {value} out of range [0.0, 100.0]")
        if self.mtu is not None and not _MTU_MIN <= self.mtu <= _MTU_MAX:
            raise TopologyError(f"link mtu {self.mtu} out of range [{_MTU_MIN}, {_MTU_MAX}]")
