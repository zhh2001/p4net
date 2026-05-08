"""Runtime primitives: namespaces, links, traffic shaping."""

from p4net.runtime.exceptions import (
    LinkError,
    NamespaceError,
    P4NetError,
    PrivilegeError,
    TcError,
)
from p4net.runtime.link import VethPair
from p4net.runtime.netns import NetworkNamespace

__all__ = [
    "LinkError",
    "NamespaceError",
    "NetworkNamespace",
    "P4NetError",
    "PrivilegeError",
    "TcError",
    "VethPair",
]
