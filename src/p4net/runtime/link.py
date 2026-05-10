"""Linux veth pair primitive backed by pyroute2.

`VethPair` represents a kernel veth pair created in the root namespace.
Each side ("a"/"b") can be moved into a `NetworkNamespace`, addressed,
and brought up/down. All netlink calls run in the namespace where the
target interface currently lives.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from pyroute2 import IPRoute, NetNS

from p4net.runtime.exceptions import LinkError
from p4net.runtime.netns import NetworkNamespace

logger = logging.getLogger(__name__)

Side = Literal["a", "b"]
_SIDES: tuple[Side, ...] = ("a", "b")
_IFNAME_MAX_LEN = 15
_MTU_MIN = 68
_MTU_MAX = 65535
_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")


def _validate_ifname(name: str) -> None:
    if not isinstance(name, str):
        raise ValueError("interface name must be a string")
    if not name:
        raise ValueError("interface name must be non-empty")
    if any(ch.isspace() for ch in name):
        raise ValueError("interface name must not contain whitespace")
    if "/" in name:
        raise ValueError("interface name must not contain '/'")
    if len(name) > _IFNAME_MAX_LEN:
        raise ValueError(f"interface name longer than {_IFNAME_MAX_LEN} characters: {name!r}")


def _validate_side(side: str) -> None:
    if side not in _SIDES:
        raise ValueError(f"side must be 'a' or 'b', got {side!r}")


class VethPair:
    """A veth pair with explicit lifecycle and per-side namespace tracking."""

    def __init__(self, name_a: str, name_b: str) -> None:
        _validate_ifname(name_a)
        _validate_ifname(name_b)
        if name_a == name_b:
            raise ValueError("the two veth sides must have distinct names")
        self._names: dict[Side, str] = {"a": name_a, "b": name_b}
        self._ns: dict[Side, NetworkNamespace | None] = {"a": None, "b": None}
        self._created = False

    @property
    def name_a(self) -> str:
        """Interface name on the ``a`` side of the pair."""
        return self._names["a"]

    @property
    def name_b(self) -> str:
        """Interface name on the ``b`` side of the pair."""
        return self._names["b"]

    def name_of(self, side: Side) -> str:
        """Return the interface name on ``side`` (``"a"`` or ``"b"``)."""
        _validate_side(side)
        return self._names[side]

    def namespace_of(self, side: Side) -> NetworkNamespace | None:
        """Return the namespace ``side`` currently lives in (``None`` for root)."""
        _validate_side(side)
        return self._ns[side]

    @contextmanager
    def _netlink_for(self, side: Side) -> Iterator[Any]:
        ns = self._ns[side]
        ipr: Any = IPRoute() if ns is None else NetNS(ns.name)
        try:
            yield ipr
        finally:
            ipr.close()

    @staticmethod
    def _index(ipr: Any, ifname: str) -> int:
        results = ipr.link_lookup(ifname=ifname)
        if not results:
            raise LinkError(f"interface {ifname!r} not found in this namespace")
        return int(results[0])

    def create(self) -> None:
        """Create the kernel veth pair via netlink. Both ends start in root netns.

        Raises:
            LinkError: if either interface already exists or the pair has
                already been created.
        """
        if self._created:
            raise LinkError("veth pair already created")
        with IPRoute() as ipr:
            if ipr.link_lookup(ifname=self._names["a"]):
                raise LinkError(f"interface {self._names['a']!r} already exists")
            if ipr.link_lookup(ifname=self._names["b"]):
                raise LinkError(f"interface {self._names['b']!r} already exists")
            ipr.link(
                "add",
                ifname=self._names["a"],
                kind="veth",
                peer=self._names["b"],
            )
        self._created = True
        logger.debug("created veth pair %r <-> %r", self._names["a"], self._names["b"])

    def destroy(self) -> None:
        """Delete the veth pair (both ends, regardless of namespace)."""
        if not self._created:
            raise LinkError("veth pair has not been created (or already destroyed)")
        # Deleting either side deletes both ends. We delete from side 'a'.
        side: Side = "a"
        try:
            with self._netlink_for(side) as ipr:
                idx = self._index(ipr, self._names[side])
                ipr.link("del", index=idx)
        except LinkError:
            raise
        except Exception as exc:
            raise LinkError(f"failed to destroy veth pair: {exc}") from exc
        self._created = False
        logger.debug("destroyed veth pair %r <-> %r", self._names["a"], self._names["b"])

    def move_to_namespace(self, side: Side, ns: NetworkNamespace | None) -> None:
        """Move ``side`` of the pair into ``ns`` (or back to root if ``None``)."""
        _validate_side(side)
        ifname = self._names[side]
        with self._netlink_for(side) as ipr:
            idx = self._index(ipr, ifname)
            if ns is None:
                # Move back to the namespace of pid 1 (the host root netns).
                ipr.link("set", index=idx, net_ns_pid=1)
            else:
                ns_path = f"/var/run/netns/{ns.name}"
                fd = os.open(ns_path, os.O_RDONLY)
                try:
                    ipr.link("set", index=idx, net_ns_fd=fd)
                finally:
                    os.close(fd)
        self._ns[side] = ns
        logger.debug("moved %r to namespace %r", ifname, ns.name if ns is not None else "<root>")

    def set_up(self, side: Side) -> None:
        """Bring ``side`` of the pair administratively up."""
        self._set_state(side, "up")

    def set_down(self, side: Side) -> None:
        """Bring ``side`` of the pair administratively down."""
        self._set_state(side, "down")

    def _set_state(self, side: Side, state: str) -> None:
        _validate_side(side)
        ifname = self._names[side]
        with self._netlink_for(side) as ipr:
            idx = self._index(ipr, ifname)
            ipr.link("set", index=idx, state=state)
        logger.debug("set %r %s", ifname, state)

    def set_address(self, side: Side, cidr: str) -> None:
        """Assign an IPv4 CIDR (e.g. ``"10.0.0.1/24"``) to ``side``."""
        _validate_side(side)
        try:
            iface_addr = ipaddress.IPv4Interface(cidr)
        except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError) as exc:
            raise ValueError(f"invalid IPv4 CIDR: {cidr!r}") from exc
        ifname = self._names[side]
        with self._netlink_for(side) as ipr:
            idx = self._index(ipr, ifname)
            ipr.addr(
                "add",
                index=idx,
                address=str(iface_addr.ip),
                prefixlen=iface_addr.network.prefixlen,
            )
        logger.debug("assigned %s to %r", cidr, ifname)

    def set_address6(self, side: Side, cidr: str) -> None:
        """Assign an IPv6 CIDR (e.g. ``"fd00::1/64"``) to one side of the pair.

        Implementation: same pyroute2 IPRoute path as `set_address`, but with
        ``family=socket.AF_INET6``. Validates the input via
        ``ipaddress.IPv6Interface`` and rejects IPv4 strings (their `/0`
        through `/32` masks would parse as IPv6Interface only by accident).
        Raises :class:`LinkError` on assignment failure.
        """
        _validate_side(side)
        if isinstance(cidr, str) and "." in cidr:
            raise ValueError(f"expected IPv6 CIDR, got IPv4-shaped {cidr!r}")
        try:
            iface_addr = ipaddress.IPv6Interface(cidr)
        except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError) as exc:
            raise ValueError(f"invalid IPv6 CIDR: {cidr!r}") from exc
        ifname = self._names[side]
        try:
            with self._netlink_for(side) as ipr:
                idx = self._index(ipr, ifname)
                ipr.addr(
                    "add",
                    index=idx,
                    address=str(iface_addr.ip),
                    prefixlen=iface_addr.network.prefixlen,
                    family=socket.AF_INET6,
                )
        except LinkError:
            raise
        except Exception as exc:
            raise LinkError(f"failed to assign IPv6 {cidr!r} to {ifname!r}: {exc}") from exc
        logger.debug("assigned %s to %r", cidr, ifname)

    def set_mtu(self, side: Side, mtu: int) -> None:
        """Set the MTU on ``side`` (clamped to ``[68, 65535]``)."""
        _validate_side(side)
        if not isinstance(mtu, int) or isinstance(mtu, bool):
            raise ValueError(f"MTU must be an int, got {type(mtu).__name__}")
        if not _MTU_MIN <= mtu <= _MTU_MAX:
            raise ValueError(f"MTU must be in [{_MTU_MIN}, {_MTU_MAX}], got {mtu}")
        ifname = self._names[side]
        with self._netlink_for(side) as ipr:
            idx = self._index(ipr, ifname)
            ipr.link("set", index=idx, mtu=mtu)
        logger.debug("set MTU of %r to %d", ifname, mtu)

    def set_mac(self, side: Side, mac: str) -> None:
        """Override the MAC on ``side`` (canonical ``XX:XX:XX:XX:XX:XX``)."""
        _validate_side(side)
        if not isinstance(mac, str) or not _MAC_RE.match(mac):
            raise ValueError(f"invalid MAC address: {mac!r}")
        ifname = self._names[side]
        with self._netlink_for(side) as ipr:
            idx = self._index(ipr, ifname)
            ipr.link("set", index=idx, address=mac.lower())
        logger.debug("set MAC of %r to %s", ifname, mac)

    def __repr__(self) -> str:
        return f"VethPair({self._names['a']!r}, {self._names['b']!r})"
