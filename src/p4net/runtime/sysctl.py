"""Configure per-interface IPv6 settings with `sysctl`.

Pass a `NetworkNamespace` to apply settings in that namespace or `None`
to use the caller's network namespace.
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

    Repeated calls are safe. Pass ``None`` as ``ns`` to use the caller's
    network namespace.
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

    Pass ``None`` as ``ns`` to use the caller's network namespace.
    By default, the interface ignores router advertisements and disables
    address autoconfiguration from advertised prefixes. The kernel can
    still generate a link-local address according to ``addr_gen_mode``.
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
