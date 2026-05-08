"""Topology description layer: pure data classes, no syscalls."""

from p4net.topo.exceptions import TopologyError
from p4net.topo.host import Host
from p4net.topo.link import Link, LinkEndpoint
from p4net.topo.switch import P4Switch
from p4net.topo.topology import Topology

__all__ = [
    "Host",
    "Link",
    "LinkEndpoint",
    "P4Switch",
    "Topology",
    "TopologyError",
]
