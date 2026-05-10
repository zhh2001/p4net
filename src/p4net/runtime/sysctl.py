"""Per-interface sysctl helpers for IPv6 gating.

These functions toggle ``net.ipv6.conf.<iface>.{disable_ipv6,accept_ra,
autoconf}`` inside a target namespace (or the root namespace if ``ns`` is
``None``). They run ``sysctl -w`` via ``ns.exec``, which inherits the same
``ip netns exec`` path used elsewhere in the runtime.

These are NOT methods on :class:`NetworkNamespace` because they target a
specific interface within a namespace, not the namespace itself.
"""

from __future__ import annotations

import logging

from p4net.runtime.exceptions import NamespaceError
from p4net.runtime.netns import NetworkNamespace

logger = logging.getLogger(__name__)


def _set_one(ns: NetworkNamespace | None, key: str, value: str) -> None:
    argv = ["sysctl", "-w", f"{key}={value}"]
    try:
        if ns is None:
            import subprocess

            result = subprocess.run(argv, capture_output=True, check=False)
        else:
            result = ns.exec(argv, capture_output=True, check=False)
    except Exception as exc:
        raise NamespaceError(f"failed to set sysctl {key}={value}: {exc}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        raise NamespaceError(
            f"sysctl {key}={value} failed (rc={result.returncode}): {stderr.strip()}"
        )


def disable_ipv6(ns: NetworkNamespace | None, iface: str) -> None:
    """Set ``net.ipv6.conf.<iface>.disable_ipv6=1`` inside ``ns``.

    Idempotent — sysctl ``-w`` to an already-set value is harmless. Pass
    ``ns=None`` to target the root namespace.
    """
    _set_one(ns, f"net.ipv6.conf.{iface}.disable_ipv6", "1")
    logger.debug("disabled IPv6 on %r in ns=%r", iface, ns.name if ns else "<root>")


def enable_ipv6(
    ns: NetworkNamespace | None,
    iface: str,
    *,
    accept_ra: bool = False,
    autoconf: bool = False,
) -> None:
    """Set ``disable_ipv6=0`` plus ``accept_ra`` and ``autoconf`` on ``iface``.

    Defaults turn off Router Advertisement handling and SLAAC autoconfiguration
    so the host only carries addresses we explicitly assign. Set
    ``accept_ra=True`` or ``autoconf=True`` if you want kernel-level
    auto-addressing on this interface.
    """
    _set_one(ns, f"net.ipv6.conf.{iface}.disable_ipv6", "0")
    _set_one(ns, f"net.ipv6.conf.{iface}.accept_ra", "1" if accept_ra else "0")
    _set_one(ns, f"net.ipv6.conf.{iface}.autoconf", "1" if autoconf else "0")
    logger.debug(
        "enabled IPv6 on %r in ns=%r (accept_ra=%s, autoconf=%s)",
        iface,
        ns.name if ns else "<root>",
        accept_ra,
        autoconf,
    )


__all__ = ["disable_ipv6", "enable_ipv6"]
