"""Topology-layer exception hierarchy."""

from __future__ import annotations

from p4net.runtime.exceptions import P4NetError


class TopologyError(P4NetError):
    """Topology validation or construction failure."""
