"""`Host` descriptor: a network node attached to switches via veth links."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from p4net.topo.exceptions import TopologyError

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,11}$")
_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")


@dataclass(frozen=True)
class Host:
    """A network host with optional primary IP, MAC, and default route.

    IPv4 lives in ``ip`` / ``default_route``; IPv6 lives in ``ip6`` /
    ``default_route6``. Either, neither, or both may be set.
    """

    name: str
    ip: str | None = None
    mac: str | None = None
    default_route: str | None = None
    ip6: str | None = None
    default_route6: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not NAME_RE.match(self.name):
            raise TopologyError(f"invalid host name {self.name!r}: must match {NAME_RE.pattern}")
        if self.ip is not None:
            try:
                ipaddress.IPv4Interface(self.ip)
            except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError) as exc:
                raise TopologyError(
                    f"invalid host IP {self.ip!r}: must be an IPv4 CIDR (e.g. '10.0.0.1/24')"
                ) from exc
        if self.mac is not None and not _MAC_RE.match(self.mac):
            raise TopologyError(f"invalid host MAC {self.mac!r}: must be 'XX:XX:XX:XX:XX:XX'")
        if self.default_route is not None:
            if self.ip is None:
                raise TopologyError(f"host {self.name!r}: default_route requires ip to be set")
            try:
                ipaddress.IPv4Address(self.default_route)
            except (ValueError, ipaddress.AddressValueError) as exc:
                raise TopologyError(
                    f"invalid default_route {self.default_route!r}: must be an IPv4 address"
                ) from exc
        if self.ip6 is not None:
            if isinstance(self.ip6, str) and "." in self.ip6:
                raise TopologyError(
                    f"invalid host ip6 {self.ip6!r}: must be an IPv6 CIDR (e.g. 'fd00::1/64')"
                )
            try:
                ipaddress.IPv6Interface(self.ip6)
            except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError) as exc:
                raise TopologyError(
                    f"invalid host ip6 {self.ip6!r}: must be an IPv6 CIDR (e.g. 'fd00::1/64')"
                ) from exc
        if self.default_route6 is not None:
            if self.ip6 is None:
                raise TopologyError(f"host {self.name!r}: default_route6 requires ip6 to be set")
            try:
                ipaddress.IPv6Address(self.default_route6)
            except (ValueError, ipaddress.AddressValueError) as exc:
                raise TopologyError(
                    f"invalid default_route6 {self.default_route6!r}: must be an IPv6 address"
                ) from exc
