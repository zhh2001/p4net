"""End-to-end topology orchestrator.

`Network.start()` brings a `Topology` up step by step (compile P4, create
namespaces, wire veth pairs, address them, apply impairments, launch BMv2,
connect P4Runtime, push pipelines). Any failure rolls back partial state
via `_do_stop()` before re-raising. `Network.stop()` is fully idempotent.

Cleanup safety net: the `_cleanup` module installs an atexit hook plus
SIGINT/SIGTERM handlers (on the main thread only) that call `stop()` on
every running `Network` before re-delivering the signal.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType

from p4net.compiler import CompileResult, P4Compiler
from p4net.control import P4RuntimeClient
from p4net.network._cleanup import (
    install_handlers,
    register,
    unregister,
)
from p4net.network.exceptions import (
    NetworkAlreadyRunningError,
    NodeNotFoundError,
)
from p4net.network.nodes import RunningHost, RunningSwitch
from p4net.runtime import (
    BMv2Switch,
    NetworkNamespace,
    VethPair,
    apply_netem,
)
from p4net.topo import Host, Link, Topology

logger = logging.getLogger(__name__)


class Network:
    """Brings up a P4 SDN topology end-to-end."""

    def __init__(
        self,
        topology: Topology,
        *,
        compiler: P4Compiler | None = None,
        log_dir: Path | None = None,
        pcap_dir: Path | None = None,
        unsafe: bool = False,
    ) -> None:
        self._topology = topology
        self._compiler = compiler if compiler is not None else P4Compiler()
        self._log_dir_explicit = log_dir
        self._log_dir: Path | None = None
        self._pcap_dir = pcap_dir
        self._unsafe = unsafe
        self._running = False
        self._registered = False

        self._namespaces: dict[str, NetworkNamespace] = {}
        self._veth_pairs: list[VethPair] = []
        self._compile_results: dict[str, CompileResult] = {}
        self._bmv2_switches: dict[str, BMv2Switch] = {}
        self._clients: dict[str, P4RuntimeClient] = {}
        self._running_hosts: dict[str, RunningHost] = {}
        self._running_switches: dict[str, RunningSwitch] = {}
        self._host_iface_ip: dict[str, dict[str, str | None]] = {}

    # Read-only views ----------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def topology(self) -> Topology:
        return self._topology

    @property
    def hosts(self) -> Mapping[str, RunningHost]:
        return self._running_hosts

    @property
    def switches(self) -> Mapping[str, RunningSwitch]:
        return self._running_switches

    @property
    def log_dir(self) -> Path:
        if self._log_dir is None:
            raise RuntimeError("log_dir is not yet allocated; call start() first")
        return self._log_dir

    def host(self, name: str) -> RunningHost:
        rh = self._running_hosts.get(name)
        if rh is None:
            raise NodeNotFoundError(f"no running host named {name!r}")
        return rh

    def switch(self, name: str) -> RunningSwitch:
        rs = self._running_switches.get(name)
        if rs is None:
            raise NodeNotFoundError(f"no running switch named {name!r}")
        return rs

    # Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._running:
            raise NetworkAlreadyRunningError("Network is already running")
        try:
            self._do_start()
            self._running = True
        except BaseException:
            self._do_stop()
            raise

    def stop(self) -> None:
        self._do_stop()

    # Ping helpers -------------------------------------------------------

    def ping(
        self,
        src: str | RunningHost,
        dst: str | RunningHost,
        *,
        count: int = 1,
        timeout: float = 2.0,
    ) -> bool:
        """Run a single ping from `src` to `dst`.

        `src` must resolve to a host in this network. `dst` may be a
        `RunningHost`, the name of a host in this network, or a literal IP
        address (anything else is passed verbatim to the underlying ping).
        Returns True iff at least one reply arrived.
        """
        src_host = src if isinstance(src, RunningHost) else self.host(src)
        if isinstance(dst, RunningHost):
            return src_host.ping(dst, count=count, timeout=timeout)
        # Resolve string `dst`: host name first, otherwise pass through as IP.
        target_host = self._running_hosts.get(dst)
        if target_host is not None:
            return src_host.ping(target_host, count=count, timeout=timeout)
        return src_host.ping(dst, count=count, timeout=timeout)

    def pingall(
        self,
        *,
        count: int = 1,
        timeout: float = 2.0,
    ) -> dict[tuple[str, str], bool]:
        """Run pings between every ordered pair of distinct hosts that have a primary IP."""
        eligible = {name: rh for name, rh in self._running_hosts.items() if rh.primary_ip}
        result: dict[tuple[str, str], bool] = {}
        for src_name, src_host in eligible.items():
            for dst_name, dst_host in eligible.items():
                if src_name == dst_name:
                    continue
                result[(src_name, dst_name)] = src_host.ping(dst_host, count=count, timeout=timeout)
        return result

    # ----- Internal start/stop ------------------------------------------

    def _do_start(self) -> None:
        # 1. validate topology
        if not self._unsafe:
            self._topology.validate()

        # 3. log/pcap dirs
        if self._log_dir_explicit is not None:
            self._log_dir = Path(self._log_dir_explicit)
            self._log_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._log_dir = Path(tempfile.mkdtemp(prefix="p4net-"))
        if self._pcap_dir is not None:
            self._pcap_dir.mkdir(parents=True, exist_ok=True)

        # 4. compile each switch's P4 source
        for sw_name, sw in self._topology.switches.items():
            self._compile_results[sw_name] = self._compiler.compile(sw.p4_src, arch=sw.arch)

        # 5. cleanup hooks
        install_handlers()
        register(self)
        self._registered = True

        # 6. host namespaces; bring lo up
        for h_name in self._topology.hosts:
            ns = NetworkNamespace(h_name)
            ns.create()
            self._namespaces[h_name] = ns
            ns.exec(["ip", "link", "set", "lo", "up"])

        # 7. veth pairs + addressing + impairment
        first_link_seen: set[str] = set()
        for h_name in self._topology.hosts:
            self._host_iface_ip[h_name] = {}
        for link in self._topology.links:
            self._wire_link(link, first_link_seen)

        # 8. default routes for hosts
        for h_name, host in self._topology.hosts.items():
            if host.default_route:
                self._namespaces[h_name].exec(
                    ["ip", "route", "add", "default", "via", host.default_route]
                )

        # 9. BMv2 switches
        for sw_name, sw in self._topology.switches.items():
            port_to_iface = self._port_to_iface_for(sw_name)
            compile_result = self._compile_results[sw_name]
            assert self._log_dir is not None
            bmv2 = BMv2Switch(
                sw_name,
                device_id=int(sw.device_id) if sw.device_id is not None else 0,
                grpc_port=int(sw.grpc_port) if sw.grpc_port is not None else 50051,
                thrift_port=int(sw.thrift_port) if sw.thrift_port is not None else 9090,
                bmv2_json=compile_result.bmv2_json,
                port_to_iface=port_to_iface,
                log_dir=self._log_dir,
                pcap_dir=self._pcap_dir,
                cpu_port=sw.cpu_port,
                log_level=sw.log_level,
            )
            bmv2.start()
            self._bmv2_switches[sw_name] = bmv2
            bmv2.wait_until_ready()

        # 10. P4Runtime clients
        election_id = (int(time.time_ns() // 1_000_000), 0)
        for sw_name, bmv2 in self._bmv2_switches.items():
            sw = self._topology.switches[sw_name]
            client = P4RuntimeClient(
                bmv2.grpc_address,
                device_id=int(sw.device_id) if sw.device_id is not None else 0,
                election_id=election_id,
            )
            client.connect()
            self._clients[sw_name] = client
            client.set_pipeline_config(
                bmv2_json=self._compile_results[sw_name].bmv2_json,
                p4info=self._compile_results[sw_name].p4info,
            )

        # 11. RunningHost / RunningSwitch facades
        for h_name, host in self._topology.hosts.items():
            self._running_hosts[h_name] = RunningHost(
                host,
                self._namespaces[h_name],
                self._host_iface_ip[h_name],
            )
        for sw_name, sw in self._topology.switches.items():
            self._running_switches[sw_name] = RunningSwitch(
                sw,
                self._bmv2_switches[sw_name],
                self._clients[sw_name],
                self._compile_results[sw_name],
            )

    def _wire_link(self, link: Link, first_link_seen: set[str]) -> None:
        name_a = link.a.iface_name
        name_b = link.b.iface_name
        assert name_a is not None and name_b is not None, (
            "link endpoints must have iface_name resolved by Topology"
        )
        veth = VethPair(name_a, name_b)
        veth.create()
        self._veth_pairs.append(veth)

        # Move host sides into their namespaces; switch sides stay in root.
        for side, ep in (("a", link.a), ("b", link.b)):
            node = self._topology.node(ep.node)
            if isinstance(node, Host):
                veth.move_to_namespace(side, self._namespaces[node.name])  # type: ignore[arg-type]

        # Configure addresses + MAC + MTU + state.
        for side, ep in (("a", link.a), ("b", link.b)):
            node = self._topology.node(ep.node)
            ip_to_use: str | None = None
            mac_to_use: str | None = None
            if isinstance(node, Host):
                if ep.ip is not None:
                    ip_to_use = ep.ip
                elif node.ip is not None and node.name not in first_link_seen:
                    ip_to_use = node.ip
                if ep.mac is not None:
                    mac_to_use = ep.mac
                elif node.mac is not None and node.name not in first_link_seen:
                    mac_to_use = node.mac
                first_link_seen.add(node.name)
                self._host_iface_ip[node.name][ep.iface_name or ""] = ip_to_use
            elif ep.mac is not None:
                # Switch endpoint MAC override is allowed (phase 2 spec).
                mac_to_use = ep.mac

            if ip_to_use is not None:
                veth.set_address(side, ip_to_use)  # type: ignore[arg-type]
            if mac_to_use is not None:
                veth.set_mac(side, mac_to_use)  # type: ignore[arg-type]
            if link.mtu is not None:
                veth.set_mtu(side, link.mtu)  # type: ignore[arg-type]
            veth.set_up(side)  # type: ignore[arg-type]

        # Apply netem impairment on both sides (symmetric shaping).
        if any(x is not None for x in (link.bandwidth, link.delay, link.jitter, link.loss_pct)):
            for ep in (link.a, link.b):
                node = self._topology.node(ep.node)
                ns = self._namespaces[node.name] if isinstance(node, Host) else None
                assert ep.iface_name is not None
                apply_netem(
                    ns,
                    ep.iface_name,
                    rate=link.bandwidth,
                    delay=link.delay,
                    jitter=link.jitter,
                    loss_pct=link.loss_pct,
                )

    def _port_to_iface_for(self, switch_name: str) -> dict[int, str]:
        result: dict[int, str] = {}
        for link in self._topology.links:
            for ep in (link.a, link.b):
                if ep.node == switch_name and ep.port is not None and ep.iface_name is not None:
                    result[int(ep.port)] = ep.iface_name
        return result

    def _do_stop(self) -> None:
        # 1. P4Runtime clients
        for sw_name, client in list(self._clients.items()):
            try:
                client.disconnect()
            except Exception as exc:
                logger.warning("disconnect %r: %r", sw_name, exc)
        self._clients.clear()

        # 2. BMv2 switches
        for sw_name, bmv2 in list(self._bmv2_switches.items()):
            try:
                bmv2.stop()
            except Exception as exc:
                logger.warning("BMv2 stop %r: %r", sw_name, exc)
        self._bmv2_switches.clear()

        # 3. veth pairs
        for veth in list(self._veth_pairs):
            try:
                veth.destroy()
            except Exception as exc:
                logger.warning("veth destroy %r: %r", veth, exc)
        self._veth_pairs.clear()

        # 4. namespaces
        for h_name, ns in list(self._namespaces.items()):
            try:
                if ns.exists:
                    ns.destroy()
            except Exception as exc:
                logger.warning("ns destroy %r: %r", h_name, exc)
        self._namespaces.clear()

        # 5. unregister hooks
        if self._registered:
            with contextlib.suppress(Exception):
                unregister(self)
            self._registered = False

        # 6. clear facades + flag
        self._running_hosts.clear()
        self._running_switches.clear()
        self._compile_results.clear()
        self._host_iface_ip.clear()
        self._running = False

    # Context manager ----------------------------------------------------

    def __enter__(self) -> Network:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


__all__ = ["Network"]
