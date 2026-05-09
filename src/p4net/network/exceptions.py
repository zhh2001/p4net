"""Exception hierarchy for the orchestration layer."""

from __future__ import annotations

from p4net.runtime.exceptions import P4NetError


class NetworkError(P4NetError):
    """Base class for orchestration-layer failures."""


class NetworkAlreadyRunningError(NetworkError):
    """`start()` called on a Network that is already running."""


class NetworkNotRunningError(NetworkError):
    """An operation that requires a running network was called before `start()`."""


class NodeNotFoundError(NetworkError):
    """No host or switch with the requested name exists in the network."""
