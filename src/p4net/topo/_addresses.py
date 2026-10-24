"""Select the addresses configured on a host link endpoint."""

from __future__ import annotations

from p4net.topo.host import Host
from p4net.topo.link import LinkEndpoint


def _effective_host_addresses(
    host: Host, endpoint: LinkEndpoint, *, first_link: bool
) -> tuple[str | None, str | None]:
    ipv4_default = host.ip if first_link else None
    ipv6_default = host.ip6 if first_link else None
    return (
        endpoint.ip if endpoint.ip is not None else ipv4_default,
        endpoint.ip6 if endpoint.ip6 is not None else ipv6_default,
    )
