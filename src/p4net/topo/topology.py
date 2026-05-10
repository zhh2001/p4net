"""`Topology`: a builder for `Host`/`P4Switch`/`Link` descriptions.

A `Topology` is pure data: it does not touch namespaces, devices, or processes.
The runtime layer (added in a later phase) will consume a validated `Topology`
and bring it up.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from p4net.topo.exceptions import TopologyError
from p4net.topo.host import Host
from p4net.topo.link import Link, LinkEndpoint
from p4net.topo.switch import P4Switch

NodeRef = str | Host | P4Switch
NodeKind = Host | P4Switch

_IFNAME_MAX_LEN = 15
_BASE_GRPC_PORT = 50051
_BASE_THRIFT_PORT = 9090


def _iface_name(node_name: str, port: int) -> str:
    return f"{node_name}-eth{port}"


def _host_to_dict(host: Host) -> dict[str, Any]:
    return {
        "name": host.name,
        "ip": host.ip,
        "mac": host.mac,
        "default_route": host.default_route,
        "ip6": host.ip6,
        "default_route6": host.default_route6,
    }


def _switch_to_dict(sw: P4Switch) -> dict[str, Any]:
    return {
        "name": sw.name,
        "p4_src": str(sw.p4_src),
        "arch": sw.arch,
        "device_id": sw.device_id,
        "grpc_port": sw.grpc_port,
        "thrift_port": sw.thrift_port,
        "cpu_port": sw.cpu_port,
        "log_level": sw.log_level,
        "pcap_enabled": sw.pcap_enabled,
    }


def _endpoint_to_dict(ep: LinkEndpoint) -> dict[str, Any]:
    return {
        "node": ep.node,
        "port": ep.port,
        "iface_name": ep.iface_name,
        "ip": ep.ip,
        "mac": ep.mac,
        "ip6": ep.ip6,
    }


def _link_to_dict(link: Link) -> dict[str, Any]:
    return {
        "a": _endpoint_to_dict(link.a),
        "b": _endpoint_to_dict(link.b),
        "bandwidth": link.bandwidth,
        "delay": link.delay,
        "jitter": link.jitter,
        "loss_pct": link.loss_pct,
        "mtu": link.mtu,
        "bandwidth_a_to_b": link.bandwidth_a_to_b,
        "bandwidth_b_to_a": link.bandwidth_b_to_a,
        "delay_a_to_b": link.delay_a_to_b,
        "delay_b_to_a": link.delay_b_to_a,
        "jitter_a_to_b": link.jitter_a_to_b,
        "jitter_b_to_a": link.jitter_b_to_a,
        "loss_pct_a_to_b": link.loss_pct_a_to_b,
        "loss_pct_b_to_a": link.loss_pct_b_to_a,
    }


class Topology:
    """A mutable builder for a topology description."""

    def __init__(self) -> None:
        self._hosts: dict[str, Host] = {}
        self._switches: dict[str, P4Switch] = {}
        self._links: list[Link] = []

    @property
    def hosts(self) -> Mapping[str, Host]:
        return self._hosts

    @property
    def switches(self) -> Mapping[str, P4Switch]:
        return self._switches

    @property
    def links(self) -> Sequence[Link]:
        return tuple(self._links)

    def node(self, name: str) -> NodeKind:
        if name in self._hosts:
            return self._hosts[name]
        if name in self._switches:
            return self._switches[name]
        raise TopologyError(f"no node named {name!r}")

    def add_host(
        self,
        name: str,
        *,
        ip: str | None = None,
        mac: str | None = None,
        default_route: str | None = None,
        ip6: str | None = None,
        default_route6: str | None = None,
    ) -> Host:
        self._reject_existing_name(name)
        host = Host(
            name=name,
            ip=ip,
            mac=mac,
            default_route=default_route,
            ip6=ip6,
            default_route6=default_route6,
        )
        self._hosts[name] = host
        return host

    def add_switch(
        self,
        name: str,
        p4_src: Path,
        *,
        arch: str = "v1model",
        device_id: int | None = None,
        grpc_port: int | None = None,
        thrift_port: int | None = None,
        cpu_port: int | None = None,
        log_level: str = "info",
        pcap_enabled: bool = True,
    ) -> P4Switch:
        self._reject_existing_name(name)
        idx = len(self._switches)
        if device_id is None:
            device_id = idx
        if grpc_port is None:
            grpc_port = _BASE_GRPC_PORT + idx
        if thrift_port is None:
            thrift_port = _BASE_THRIFT_PORT + idx
        switch = P4Switch(
            name=name,
            p4_src=p4_src,
            arch=arch,
            device_id=device_id,
            grpc_port=grpc_port,
            thrift_port=thrift_port,
            cpu_port=cpu_port,
            log_level=log_level,
            pcap_enabled=pcap_enabled,
        )
        self._switches[name] = switch
        return switch

    def add_link(
        self,
        a: NodeRef,
        b: NodeRef,
        *,
        port_a: int | None = None,
        port_b: int | None = None,
        ip_a: str | None = None,
        ip_b: str | None = None,
        mac_a: str | None = None,
        mac_b: str | None = None,
        ip6_a: str | None = None,
        ip6_b: str | None = None,
        bandwidth: str | None = None,
        delay: str | None = None,
        jitter: str | None = None,
        loss_pct: float | None = None,
        mtu: int | None = None,
        bandwidth_a_to_b: str | None = None,
        bandwidth_b_to_a: str | None = None,
        delay_a_to_b: str | None = None,
        delay_b_to_a: str | None = None,
        jitter_a_to_b: str | None = None,
        jitter_b_to_a: str | None = None,
        loss_pct_a_to_b: float | None = None,
        loss_pct_b_to_a: float | None = None,
    ) -> Link:
        node_a = self._resolve(a)
        node_b = self._resolve(b)
        port_a_val = self._auto_port(node_a, port_a)
        port_b_val = self._auto_port(node_b, port_b)
        iface_a = _iface_name(node_a.name, port_a_val)
        iface_b = _iface_name(node_b.name, port_b_val)
        if len(iface_a) > _IFNAME_MAX_LEN:
            raise TopologyError(
                f"interface name {iface_a!r} exceeds {_IFNAME_MAX_LEN} chars; shorten the node name"
            )
        if len(iface_b) > _IFNAME_MAX_LEN:
            raise TopologyError(
                f"interface name {iface_b!r} exceeds {_IFNAME_MAX_LEN} chars; shorten the node name"
            )
        if ip_a is not None and isinstance(node_a, P4Switch):
            raise TopologyError(
                "P4 switch data ports do not carry IP addresses; remove ip_a from the add_link call"
            )
        if ip_b is not None and isinstance(node_b, P4Switch):
            raise TopologyError(
                "P4 switch data ports do not carry IP addresses; remove ip_b from the add_link call"
            )
        if ip6_a is not None and isinstance(node_a, P4Switch):
            raise TopologyError(
                "P4 switch data ports do not carry IP addresses; "
                "remove ip6_a from the add_link call"
            )
        if ip6_b is not None and isinstance(node_b, P4Switch):
            raise TopologyError(
                "P4 switch data ports do not carry IP addresses; "
                "remove ip6_b from the add_link call"
            )
        ep_a = LinkEndpoint(
            node=node_a.name,
            port=port_a_val,
            iface_name=iface_a,
            ip=ip_a,
            mac=mac_a,
            ip6=ip6_a,
        )
        ep_b = LinkEndpoint(
            node=node_b.name,
            port=port_b_val,
            iface_name=iface_b,
            ip=ip_b,
            mac=mac_b,
            ip6=ip6_b,
        )
        link = Link(
            a=ep_a,
            b=ep_b,
            bandwidth=bandwidth,
            delay=delay,
            jitter=jitter,
            loss_pct=loss_pct,
            mtu=mtu,
            bandwidth_a_to_b=bandwidth_a_to_b,
            bandwidth_b_to_a=bandwidth_b_to_a,
            delay_a_to_b=delay_a_to_b,
            delay_b_to_a=delay_b_to_a,
            jitter_a_to_b=jitter_a_to_b,
            jitter_b_to_a=jitter_b_to_a,
            loss_pct_a_to_b=loss_pct_a_to_b,
            loss_pct_b_to_a=loss_pct_b_to_a,
        )
        self._links.append(link)
        return link

    def neighbors_of(self, name: str) -> list[tuple[Link, LinkEndpoint, LinkEndpoint]]:
        """Return every link involving `name`, as (link, near, far)."""
        result: list[tuple[Link, LinkEndpoint, LinkEndpoint]] = []
        for link in self._links:
            if link.a.node == name:
                result.append((link, link.a, link.b))
            elif link.b.node == name:
                result.append((link, link.b, link.a))
        return result

    def port_assignments(self, switch_name: str) -> dict[int, LinkEndpoint]:
        """Map of switch port number to the FAR endpoint connected on that port."""
        if switch_name not in self._switches:
            raise TopologyError(f"no switch named {switch_name!r}")
        result: dict[int, LinkEndpoint] = {}
        for link in self._links:
            if link.a.node == switch_name and link.a.port is not None:
                result[link.a.port] = link.b
            elif link.b.node == switch_name and link.b.port is not None:
                result[link.b.port] = link.a
        return result

    def validate(self) -> None:
        """Raise `TopologyError` listing every internal-consistency problem found."""
        errors: list[str] = []

        # 1. Endpoint references resolve to a known node.
        # 2. No self-loops.
        for i, link in enumerate(self._links):
            for which, ep in (("a", link.a), ("b", link.b)):
                if ep.node not in self._hosts and ep.node not in self._switches:
                    errors.append(f"link[{i}] endpoint {which} references unknown node {ep.node!r}")
            if link.a.node == link.b.node:
                errors.append(f"link[{i}] connects node {link.a.node!r} to itself")

        # 3. No (node, port) pair repeats across links.
        seen: dict[tuple[str, int], int] = {}
        for i, link in enumerate(self._links):
            for _which, ep in (("a", link.a), ("b", link.b)):
                if ep.port is None:
                    continue
                key = (ep.node, ep.port)
                if key in seen:
                    errors.append(
                        f"port collision: ({ep.node!r}, port {ep.port}) used by "
                        f"link[{seen[key]}] and link[{i}]"
                    )
                else:
                    seen[key] = i

        # 4. No two switches share device_id, grpc_port, or thrift_port.
        for field_name in ("device_id", "grpc_port", "thrift_port"):
            owners: dict[Any, str] = {}
            for sw in self._switches.values():
                value = getattr(sw, field_name)
                if value is None:
                    continue
                if value in owners:
                    errors.append(
                        f"switch {field_name} collision: {sw.name!r} and "
                        f"{owners[value]!r} both use {value}"
                    )
                else:
                    owners[value] = sw.name

        # 5. Interface name length.
        for i, link in enumerate(self._links):
            for which, ep in (("a", link.a), ("b", link.b)):
                if ep.iface_name is not None and len(ep.iface_name) > _IFNAME_MAX_LEN:
                    errors.append(
                        f"link[{i}] endpoint {which}: interface name "
                        f"{ep.iface_name!r} exceeds {_IFNAME_MAX_LEN} chars"
                    )

        # 6. No two hosts share an IP within the same IPv4Network.
        # Collect (node_name, IPv4Interface) from host primary IPs and link
        # overrides, group by network, flag any (network, address) used by
        # more than one distinct node.
        per_network: dict[ipaddress.IPv4Network, dict[ipaddress.IPv4Address, set[str]]] = {}
        for host in self._hosts.values():
            if host.ip is None:
                continue
            try:
                iface = ipaddress.IPv4Interface(host.ip)
            except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError):
                continue  # already validated at construction; ignore here
            per_network.setdefault(iface.network, {}).setdefault(iface.ip, set()).add(host.name)
        for link in self._links:
            for ep in (link.a, link.b):
                if ep.ip is None:
                    continue
                if ep.node not in self._hosts:
                    continue  # switch endpoints with IPs are rejected at add_link
                try:
                    iface = ipaddress.IPv4Interface(ep.ip)
                except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError):
                    errors.append(f"link endpoint {ep.node!r}: invalid IP {ep.ip!r}")
                    continue
                per_network.setdefault(iface.network, {}).setdefault(iface.ip, set()).add(ep.node)
        for network, addrs in per_network.items():
            for addr, nodes in addrs.items():
                if len(nodes) > 1:
                    errors.append(
                        f"IP collision on {network}: address {addr} used by hosts {sorted(nodes)}"
                    )

        # 7. Same as 6, but for IPv6.
        per_network6: dict[ipaddress.IPv6Network, dict[ipaddress.IPv6Address, set[str]]] = {}
        for host in self._hosts.values():
            if host.ip6 is None:
                continue
            try:
                iface6 = ipaddress.IPv6Interface(host.ip6)
            except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError):
                continue
            per_network6.setdefault(iface6.network, {}).setdefault(iface6.ip, set()).add(host.name)
        for link in self._links:
            for ep in (link.a, link.b):
                if ep.ip6 is None:
                    continue
                if ep.node not in self._hosts:
                    continue
                try:
                    iface6 = ipaddress.IPv6Interface(ep.ip6)
                except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError):
                    errors.append(f"link endpoint {ep.node!r}: invalid ip6 {ep.ip6!r}")
                    continue
                per_network6.setdefault(iface6.network, {}).setdefault(iface6.ip, set()).add(
                    ep.node
                )
        for network6, addrs6 in per_network6.items():
            for addr6, nodes6 in addrs6.items():
                if len(nodes6) > 1:
                    errors.append(
                        f"IPv6 collision on {network6}: address {addr6} used by hosts "
                        f"{sorted(nodes6)}"
                    )

        if errors:
            raise TopologyError(
                "topology validation failed with the following problems:\n  - "
                + "\n  - ".join(errors)
            )

    def to_graphviz(self, *, layout: str = "LR") -> str:
        """Render the topology as a Graphviz DOT graph string.

        Hosts are ellipses labelled with name and primary IP(s); switches are
        boxes labelled with name and gRPC port. Edges are drawn with
        ``arrowhead=none`` to keep the rendering version-stable across
        graphviz releases. ``layout`` controls ``rankdir`` and must be one
        of ``"LR"``, ``"RL"``, ``"TB"``, ``"BT"``.
        """
        if layout not in {"LR", "RL", "TB", "BT"}:
            raise TopologyError(f"layout {layout!r} must be one of 'LR', 'RL', 'TB', 'BT'")
        lines: list[str] = ["digraph p4net {"]
        lines.append(f"  rankdir={layout};")
        lines.append('  node [fontname="monospace"];')
        for host in self._hosts.values():
            label_parts = [host.name]
            if host.ip is not None:
                label_parts.append(host.ip)
            if host.ip6 is not None:
                label_parts.append(host.ip6)
            label = "\\n".join(label_parts)
            lines.append(f'  "{host.name}" [shape=ellipse, label="{label}"];')
        for sw in self._switches.values():
            label_parts = [sw.name]
            if sw.grpc_port is not None:
                label_parts.append(f"grpc :{sw.grpc_port}")
            label = "\\n".join(label_parts)
            lines.append(f'  "{sw.name}" [shape=box, label="{label}"];')
        for link in self._links:
            lines.append(f'  "{link.a.node}" -> "{link.b.node}" [arrowhead=none];')
        lines.append("}")
        return "\n".join(lines) + "\n"

    def render_graphviz(
        self,
        output_path: Path,
        *,
        layout: str = "LR",
        format: str = "png",
    ) -> None:
        """Render via the system ``dot`` binary to ``output_path``.

        ``format`` is forwarded to ``dot -T<format>`` (png, svg, pdf, dot).
        For ``format="dot"`` the source is written verbatim and ``dot`` is
        not invoked, so this path works without graphviz installed.
        Raises :class:`TopologyError` if ``dot`` is missing or the render
        fails.
        """
        import shutil
        import subprocess

        source = self.to_graphviz(layout=layout)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if format == "dot":
            output_path.write_text(source)
            return
        dot_bin = shutil.which("dot")
        if dot_bin is None:
            raise TopologyError(
                "graphviz `dot` binary not found on PATH; "
                "install graphviz or use format='dot' to write the source file directly"
            )
        try:
            subprocess.run(
                [dot_bin, f"-T{format}", "-o", str(output_path)],
                input=source.encode("utf-8"),
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
            raise TopologyError(
                f"`dot -T{format}` failed (rc={exc.returncode}): {stderr.strip()}"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "hosts": {name: _host_to_dict(host) for name, host in self._hosts.items()},
            "switches": {name: _switch_to_dict(sw) for name, sw in self._switches.items()},
            "links": [_link_to_dict(link) for link in self._links],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Topology:
        topo = cls()
        for name, host_data in data.get("hosts", {}).items():
            topo._hosts[name] = Host(
                name=host_data["name"],
                ip=host_data.get("ip"),
                mac=host_data.get("mac"),
                default_route=host_data.get("default_route"),
                ip6=host_data.get("ip6"),
                default_route6=host_data.get("default_route6"),
            )
        for name, sw_data in data.get("switches", {}).items():
            topo._switches[name] = P4Switch(
                name=sw_data["name"],
                p4_src=Path(sw_data["p4_src"]),
                arch=sw_data.get("arch", "v1model"),
                device_id=sw_data.get("device_id"),
                grpc_port=sw_data.get("grpc_port"),
                thrift_port=sw_data.get("thrift_port"),
                cpu_port=sw_data.get("cpu_port"),
                log_level=sw_data.get("log_level", "info"),
                pcap_enabled=sw_data.get("pcap_enabled", True),
            )
        for link_data in data.get("links", []):
            ep_a = LinkEndpoint(
                node=link_data["a"]["node"],
                port=link_data["a"].get("port"),
                iface_name=link_data["a"].get("iface_name"),
                ip=link_data["a"].get("ip"),
                mac=link_data["a"].get("mac"),
                ip6=link_data["a"].get("ip6"),
            )
            ep_b = LinkEndpoint(
                node=link_data["b"]["node"],
                port=link_data["b"].get("port"),
                iface_name=link_data["b"].get("iface_name"),
                ip=link_data["b"].get("ip"),
                mac=link_data["b"].get("mac"),
                ip6=link_data["b"].get("ip6"),
            )
            topo._links.append(
                Link(
                    a=ep_a,
                    b=ep_b,
                    bandwidth=link_data.get("bandwidth"),
                    delay=link_data.get("delay"),
                    jitter=link_data.get("jitter"),
                    loss_pct=link_data.get("loss_pct"),
                    mtu=link_data.get("mtu"),
                    bandwidth_a_to_b=link_data.get("bandwidth_a_to_b"),
                    bandwidth_b_to_a=link_data.get("bandwidth_b_to_a"),
                    delay_a_to_b=link_data.get("delay_a_to_b"),
                    delay_b_to_a=link_data.get("delay_b_to_a"),
                    jitter_a_to_b=link_data.get("jitter_a_to_b"),
                    jitter_b_to_a=link_data.get("jitter_b_to_a"),
                    loss_pct_a_to_b=link_data.get("loss_pct_a_to_b"),
                    loss_pct_b_to_a=link_data.get("loss_pct_b_to_a"),
                )
            )
        return topo

    # Internal helpers ---------------------------------------------------

    def _reject_existing_name(self, name: str) -> None:
        if name in self._hosts or name in self._switches:
            raise TopologyError(f"node name {name!r} already exists in this topology")

    def _resolve(self, ref: NodeRef) -> NodeKind:
        if isinstance(ref, Host):
            if self._hosts.get(ref.name) is not ref:
                raise TopologyError(f"host {ref.name!r} is not part of this topology")
            return ref
        if isinstance(ref, P4Switch):
            if self._switches.get(ref.name) is not ref:
                raise TopologyError(f"switch {ref.name!r} is not part of this topology")
            return ref
        if isinstance(ref, str):
            return self.node(ref)
        raise TopologyError(
            f"node reference must be a name string, Host, or P4Switch; got {type(ref).__name__}"
        )

    def _auto_port(self, node: NodeKind, requested: int | None) -> int:
        if requested is not None:
            return requested
        used = {
            ep.port
            for link in self._links
            for ep in (link.a, link.b)
            if ep.node == node.name and ep.port is not None
        }
        candidate = 1 if isinstance(node, P4Switch) else 0
        while candidate in used:
            candidate += 1
        return candidate
