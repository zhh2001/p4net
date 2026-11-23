"""Linux veth pair primitive backed by pyroute2.

`VethPair` represents a kernel veth pair with configurable endpoint namespaces.
Each side ("a"/"b") can be moved, addressed, and brought up/down. All netlink
calls run in the namespace where the target interface currently lives.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pyroute2 import IPRoute, NetNS

from p4net._ifname import _validate_ifname
from p4net.runtime.exceptions import LinkError
from p4net.runtime.netns import NetworkNamespace

logger = logging.getLogger(__name__)

Side = Literal["a", "b"]
_SIDES: tuple[Side, ...] = ("a", "b")
_PID_ONE_NETNS = "/proc/1/ns/net"
_MTU_MIN = 68
_MTU_MAX = 65535
_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")


def _validate_side(side: str) -> None:
    if side not in _SIDES:
        raise ValueError(f"side must be 'a' or 'b', got {side!r}")


class VethPair:
    """A veth pair with explicit lifecycle and per-side namespace tracking.

    ``namespace_a`` and ``namespace_b`` select the endpoint namespaces at
    creation. An endpoint configured with ``None`` stays in the caller's
    network namespace. Interface names may repeat across distinct namespaces.
    """

    def __init__(
        self,
        name_a: str,
        name_b: str,
        *,
        namespace_a: NetworkNamespace | None = None,
        namespace_b: NetworkNamespace | None = None,
    ) -> None:
        _validate_ifname(name_a)
        _validate_ifname(name_b)
        scope_a = namespace_a.name if namespace_a is not None else None
        scope_b = namespace_b.name if namespace_b is not None else None
        if name_a == name_b and scope_a == scope_b:
            raise ValueError("the two veth sides must have distinct names within one namespace")
        self._names: dict[Side, str] = {"a": name_a, "b": name_b}
        self._targets: dict[Side, NetworkNamespace | None] = {"a": namespace_a, "b": namespace_b}
        self._ns: dict[Side, NetworkNamespace | str | None] = {"a": None, "b": None}
        self._created = False
        self._destroy_index: int | None = None

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
        """Return the tracked `NetworkNamespace` for ``side``.

        ``None`` covers absent endpoints, endpoints created in the caller's
        network namespace, and endpoints moved to PID 1's network namespace.
        """
        _validate_side(side)
        ns = self._ns[side]
        return None if isinstance(ns, str) else ns

    @contextmanager
    def _netlink_for(self, side: Side, *, allow_missing: bool = False) -> Iterator[Any]:
        ns = self._ns[side]
        try:
            if ns is None:
                ipr: Any = IPRoute()
            elif isinstance(ns, str):
                ipr = NetNS(ns, flags=0)
            else:
                ipr = NetNS(ns.name, flags=0)
        except FileNotFoundError:
            if not allow_missing:
                raise
            yield None
            return
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
        """Create the veth pair in its configured endpoint namespaces.

        Raises:
            LinkError: if either interface already exists or the pair has
                already been created.
        """
        if self._created:
            raise LinkError("veth pair already created")
        with IPRoute() as ipr:
            for side in _SIDES:
                if self._targets[side] is None and ipr.link_lookup(ifname=self._names[side]):
                    raise LinkError(f"interface {self._names[side]!r} already exists")
            if all(ns is None for ns in self._targets.values()) and all(
                name.isascii() for name in self._names.values()
            ):
                ipr.link(
                    "add",
                    ifname=self._names["a"],
                    kind="veth",
                    peer=self._names["b"],
                )
            else:
                self._create_temporary_pair(ipr)
        self._created = True
        logger.debug("created veth pair %r <-> %r", self._names["a"], self._names["b"])

    def _create_temporary_pair(self, ipr: Any) -> None:
        udevadm = None
        if (
            any(not name.isascii() for name in self._names.values())
            and Path("/run/udev/control").exists()
        ):
            udevadm = shutil.which("udevadm")
            if udevadm is None:
                raise LinkError("udevadm is required for non-ASCII interfaces when udev is active")
        prefix = "p4n" + uuid.uuid4().hex[:9]
        temporary_a, temporary_b = prefix + "a", prefix + "b"
        ipr.link("add", ifname=temporary_a, kind="veth", peer=temporary_b)
        indexes: dict[Side, int] = {}
        cleanup_side: Side = "a"
        try:
            indexes["a"] = self._index(ipr, temporary_a)
            indexes["b"] = self._index(ipr, temporary_b)
            if self._targets["a"] is not None:
                cleanup_side = "b"
            if udevadm is not None:
                try:
                    subprocess.run(
                        [udevadm, "settle", "--timeout=5"],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=6,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    raise LinkError(
                        f"cannot wait for udev interface initialization: {exc}"
                    ) from exc
            for side in _SIDES:
                target = self._targets[side]
                if target is None:
                    ipr.link("set", index=indexes[side], ifname=self._names[side])
                    cleanup_side = side
                else:
                    fd = os.open(f"/var/run/netns/{target.name}", os.O_RDONLY)
                    try:
                        # Move and name together so root names cannot block host names.
                        ipr.link("set", index=indexes[side], net_ns_fd=fd, ifname=self._names[side])
                        self._ns[side] = target
                        cleanup_side = side
                    finally:
                        os.close(fd)
        except BaseException:
            try:
                if not indexes:
                    ipr.link("del", ifname=temporary_a)
                elif self._ns[cleanup_side] is None:
                    ipr.link("del", index=indexes[cleanup_side])
                else:
                    with self._netlink_for(cleanup_side) as target_ipr:
                        index = self._index(target_ipr, self._names[cleanup_side])
                        target_ipr.link("del", index=index)
                self._ns = {"a": None, "b": None}
            except Exception:
                logger.exception("failed to remove temporary veth pair %r", temporary_a)
            raise

    def destroy(self) -> None:
        """Delete the pair if it still exists and keep the creation targets."""
        if not self._created:
            raise LinkError("veth pair has not been created (or already destroyed)")
        try:
            with self._netlink_for("a", allow_missing=True) as ipr:
                indexes = ipr.link_lookup(ifname=self.name_a) if ipr is not None else []
                if indexes:
                    index = int(indexes[0])
                    # A retry must target the interface observed on the first attempt.
                    if self._destroy_index is None or self._destroy_index == index:
                        self._destroy_index = index
                        ipr.link("del", index=index)
                else:
                    with self._netlink_for("b", allow_missing=True) as peer:
                        if peer is not None and peer.link_lookup(ifname=self.name_b):
                            raise LinkError(f"interface {self.name_a!r} not found in its namespace")
        except LinkError:
            raise
        except Exception as exc:
            raise LinkError(f"failed to destroy veth pair: {exc}") from exc
        self._created = False
        self._destroy_index = None
        self._ns = {"a": None, "b": None}
        logger.debug("destroyed veth pair %r <-> %r", self._names["a"], self._names["b"])

    def move_to_namespace(self, side: Side, ns: NetworkNamespace | None) -> None:
        """Move ``side`` of the pair into ``ns``.

        Passing ``None`` moves the endpoint into PID 1's network namespace.
        """
        _validate_side(side)
        ifname = self._names[side]
        with self._netlink_for(side) as ipr:
            idx = self._index(ipr, ifname)
            if ns is None:
                ipr.link("set", index=idx, net_ns_pid=1)
                self._ns[side] = _PID_ONE_NETNS
            else:
                # NetNS can handle requests in a separate process that needs
                # to open the destination namespace itself.
                ipr.link("set", index=idx, net_ns_fd=f"/var/run/netns/{ns.name}")
                self._ns[side] = ns
        if side == "a":
            self._destroy_index = None
        logger.debug(
            "moved %r to namespace %r", ifname, ns.name if ns is not None else _PID_ONE_NETNS
        )

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
        """Set the MTU on ``side`` to an int in ``[68, 65535]``, excluding bool."""
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
