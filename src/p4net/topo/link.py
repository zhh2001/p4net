"""`Link` and `LinkEndpoint` descriptors for a single veth pair in a topology."""

from __future__ import annotations

from dataclasses import dataclass

from p4net.topo.exceptions import TopologyError

_MTU_MIN = 68
_MTU_MAX = 65535


@dataclass(frozen=True)
class LinkEndpoint:
    """One end of a `Link`, attached to a host or switch."""

    node: str
    port: int | None = None
    iface_name: str | None = None
    ip: str | None = None


@dataclass(frozen=True)
class Link:
    """A bidirectional veth pair between two nodes, with optional impairments.

    Impairments are link-level and apply symmetrically (the runtime layer
    applies the same netem state to both veth sides). Asymmetric per-direction
    shaping is out of scope for this release.
    """

    a: LinkEndpoint
    b: LinkEndpoint
    bandwidth: str | None = None
    delay: str | None = None
    jitter: str | None = None
    loss_pct: float | None = None
    mtu: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.a, LinkEndpoint):
            raise TopologyError("Link.a must be a LinkEndpoint")
        if not isinstance(self.b, LinkEndpoint):
            raise TopologyError("Link.b must be a LinkEndpoint")
        if not self.a.node:
            raise TopologyError("Link.a.node must be a non-empty string")
        if not self.b.node:
            raise TopologyError("Link.b.node must be a non-empty string")
        if self.jitter is not None and self.delay is None:
            raise TopologyError("link jitter requires delay to be set")
        if self.loss_pct is not None and not 0.0 <= self.loss_pct <= 100.0:
            raise TopologyError(f"link loss_pct {self.loss_pct} out of range [0.0, 100.0]")
        if self.mtu is not None and not _MTU_MIN <= self.mtu <= _MTU_MAX:
            raise TopologyError(f"link mtu {self.mtu} out of range [{_MTU_MIN}, {_MTU_MAX}]")
