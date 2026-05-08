"""Exception hierarchy for the p4net runtime layer."""

from __future__ import annotations


class P4NetError(Exception):
    """Base class for all p4net-specific errors."""


class PrivilegeError(P4NetError):
    """Raised when the operation requires root or CAP_NET_ADMIN and the caller has neither."""


class NamespaceError(P4NetError):
    """Failure creating, destroying, or operating in a network namespace."""


class LinkError(P4NetError):
    """Failure creating, configuring, or destroying a network link."""


class TcError(P4NetError):
    """Failure configuring traffic control / netem."""
