"""Traffic control / netem helpers.

We shell out to the `tc` binary rather than using the pyroute2 TC API.
Rationale: `tc` is the documented public interface for qdisc/netem, has
stable command-line semantics across distributions, and produces clear
diagnostics on failure; pyroute2's TC bindings are an evolving lower-level
API that adds complexity without benefit at this layer.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import TYPE_CHECKING

from p4net.runtime.exceptions import TcError

if TYPE_CHECKING:
    from p4net.runtime.netns import NetworkNamespace

logger = logging.getLogger(__name__)

_RATE_RE = re.compile(
    r"^\d+(?:\.\d+)?\s*(?:bit|kbit|mbit|gbit|tbit|bps|kbps|mbps|gbps|tbps)$",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"^\d+(?:\.\d+)?\s*(?:s|ms|us)$", re.IGNORECASE)


def _validate_rate(rate: str) -> None:
    if not isinstance(rate, str) or not _RATE_RE.match(rate):
        raise TcError(f"invalid tc rate: {rate!r}")


def _validate_time(value: str, label: str) -> None:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise TcError(f"invalid {label}: {value!r}")


def _run(ns: NetworkNamespace | None, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    if ns is None:
        return subprocess.run(argv, capture_output=True, check=False)
    return ns.exec(argv, capture_output=True, check=False)


def apply_netem(
    ns: NetworkNamespace | None,
    iface: str,
    *,
    rate: str | None = None,
    delay: str | None = None,
    jitter: str | None = None,
    loss_pct: float | None = None,
) -> None:
    """Apply a netem root qdisc to `iface` inside `ns` (or root if ns is None).

    Uses `tc qdisc replace` so calling this function repeatedly is safe.
    At least one of `rate`, `delay`, `jitter`, `loss_pct` must be set.
    `jitter` requires `delay`.
    """
    if rate is None and delay is None and jitter is None and loss_pct is None:
        raise TcError("apply_netem requires at least one of rate, delay, jitter, loss_pct")
    if jitter is not None and delay is None:
        raise TcError("jitter requires delay")
    if rate is not None:
        _validate_rate(rate)
    if delay is not None:
        _validate_time(delay, "delay")
    if jitter is not None:
        _validate_time(jitter, "jitter")
    if loss_pct is not None and not 0.0 <= loss_pct <= 100.0:
        raise TcError(f"loss_pct must be in [0.0, 100.0], got {loss_pct}")

    argv = ["tc", "qdisc", "replace", "dev", iface, "root", "netem"]
    if delay is not None:
        argv += ["delay", delay]
        if jitter is not None:
            argv += [jitter]
    if loss_pct is not None:
        argv += ["loss", f"{loss_pct}%"]
    if rate is not None:
        argv += ["rate", rate]

    result = _run(ns, argv)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise TcError(f"tc qdisc replace failed (rc={result.returncode}): {stderr}")
    logger.debug(
        "applied netem on %r in ns=%r: %s",
        iface,
        ns.name if ns is not None else None,
        " ".join(argv[6:]),
    )


def clear_qdisc(ns: NetworkNamespace | None, iface: str) -> None:
    """Remove the root qdisc on `iface`. Idempotent: no-op if none is set."""
    argv = ["tc", "qdisc", "del", "dev", iface, "root"]
    result = _run(ns, argv)
    if result.returncode == 0:
        logger.debug("cleared qdisc on %r in ns=%r", iface, ns.name if ns is not None else None)
        return
    stderr_lc = result.stderr.decode(errors="replace").lower()
    # iproute2 reports "no such" / "cannot find" / "handle of zero" when no
    # user-installed root qdisc exists; treat all three as a successful no-op.
    if (
        "no such" in stderr_lc
        or "no qdisc" in stderr_lc
        or "cannot find" in stderr_lc
        or "handle of zero" in stderr_lc
    ):
        logger.debug(
            "no qdisc to clear on %r in ns=%r (already clean)",
            iface,
            ns.name if ns is not None else None,
        )
        return
    raise TcError(
        f"tc qdisc del failed (rc={result.returncode}): "
        f"{result.stderr.decode(errors='replace').strip()}"
    )
