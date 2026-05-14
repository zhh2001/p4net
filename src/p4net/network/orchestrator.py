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
import os
import re
import tempfile
import time
from collections.abc import Mapping, Sequence
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
    NetworkError,
    NetworkNotRunningError,
    NodeNotFoundError,
)
from p4net.network.nodes import RunningHost, RunningSwitch
from p4net.runtime import (
    BMv2Switch,
    NetworkNamespace,
    NSProcess,
    VethPair,
    apply_netem,
    disable_ipv6,
    enable_ipv6,
)
from p4net.topo import Host, Link, Topology
from p4net.topo.exceptions import TopologyError

logger = logging.getLogger(__name__)

_DURATION_UNITS_NS: dict[str, int] = {
    "ns": 1,
    "us": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
}


def _parse_duration_ns(value: str) -> int:
    """Parse a netem-style duration string (e.g. ``"100ms"``, ``"1s"``) to ns."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ns|us|ms|s)\s*", value)
    if match is None:
        raise ValueError(f"invalid duration string {value!r}")
    magnitude, unit = match.groups()
    return round(float(magnitude) * _DURATION_UNITS_NS[unit])


def _format_duration_ns(ns: int) -> str:
    """Format nanoseconds back into the largest exact unit (canonical short form)."""
    for unit, scale in (("s", 1_000_000_000), ("ms", 1_000_000), ("us", 1_000), ("ns", 1)):
        if ns % scale == 0 and ns >= scale:
            return f"{ns // scale}{unit}"
    return f"{ns}ns"


def _add_durations(base: str, extra: str) -> str:
    """Return the canonical-shortest sum of two netem duration strings."""
    return _format_duration_ns(_parse_duration_ns(base) + _parse_duration_ns(extra))


def _resolve_dir_str(
    sym: str | None,
    per_dir: str | None,
    extra: str | None,
) -> str | None:
    """Resolve a per-direction string-typed netem param (delay / jitter)."""
    if extra is not None and sym is not None:
        return _add_durations(sym, extra)
    if per_dir is not None:
        return per_dir
    return sym


def _resolve_dir_loss(
    sym: float | None,
    per_dir: float | None,
    extra: float | None,
) -> float | None:
    """Resolve a per-direction loss percentage with cap-at-100 + raise on overflow."""
    if extra is not None and sym is not None:
        total = sym + extra
        if total > 100.0:
            raise TopologyError(f"loss_pct base {sym} + extra {extra} = {total}, exceeds 100.0")
        return total
    if per_dir is not None:
        return per_dir
    return sym


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
        extra_compile_args: Sequence[str] = (),
    ) -> None:
        self._topology = topology
        self._compiler = compiler if compiler is not None else P4Compiler()
        self._log_dir_explicit = log_dir
        self._log_dir: Path | None = None
        self._pcap_dir = pcap_dir
        self._unsafe = unsafe
        self._extra_compile_args: tuple[str, ...] = tuple(extra_compile_args)
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
        self._host_iface_ip6: dict[str, dict[str, str | None]] = {}
        self._spawned_processes: list[NSProcess] = []

    # Read-only views ----------------------------------------------------

    @property
    def is_running(self) -> bool:
        """``True`` once :meth:`start` has succeeded and before :meth:`stop`."""
        return self._running

    @property
    def topology(self) -> Topology:
        """The :class:`Topology` description backing this network."""
        return self._topology

    @property
    def hosts(self) -> Mapping[str, RunningHost]:
        """Map of host name → :class:`RunningHost`. Empty until :meth:`start`."""
        return self._running_hosts

    @property
    def switches(self) -> Mapping[str, RunningSwitch]:
        """Map of switch name → :class:`RunningSwitch`. Empty until :meth:`start`."""
        return self._running_switches

    @property
    def log_dir(self) -> Path:
        """Directory where BMv2 log files are written.

        Raises:
            RuntimeError: if accessed before :meth:`start`.
        """
        if self._log_dir is None:
            raise RuntimeError("log_dir is not yet allocated; call start() first")
        return self._log_dir

    def host(self, name: str) -> RunningHost:
        """Return the :class:`RunningHost` named ``name``.

        Raises:
            NodeNotFoundError: if no such host is in this network.
        """
        rh = self._running_hosts.get(name)
        if rh is None:
            raise NodeNotFoundError(f"no running host named {name!r}")
        return rh

    def switch(self, name: str) -> RunningSwitch:
        """Return the :class:`RunningSwitch` named ``name``.

        Raises:
            NodeNotFoundError: if no such switch is in this network.
        """
        rs = self._running_switches.get(name)
        if rs is None:
            raise NodeNotFoundError(f"no running switch named {name!r}")
        return rs

    @property
    def boot_timestamps(self) -> dict[str, int]:
        """Mapping of switch name to wall-clock μs when its BMv2 started.

        Equivalent to ``{name: self.switch(name).boot_timestamp_us for name
        in self.topology.switches}``, but more concise and stays in sync
        with the running set.

        Returns:
            Fresh dict (callers may mutate it without affecting the
            network's internal state).

        Raises:
            NetworkNotRunningError: if the network has not been started or
                has been stopped.
        """
        if not self._running:
            raise NetworkNotRunningError(
                "Network is not running; call start() before boot_timestamps"
            )
        return {name: rs.boot_timestamp_us for name, rs in self._running_switches.items()}

    # Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Bring the topology up end-to-end.

        Validates the topology (unless ``unsafe=True``), compiles each P4
        source, creates host namespaces and veth pairs, configures
        addresses and impairment, launches BMv2 processes, opens
        P4Runtime clients, and pushes pipeline configs. On failure,
        rolls back via :meth:`_do_stop` before re-raising.

        Raises:
            NetworkAlreadyRunningError: if already running.
        """
        if self._running:
            raise NetworkAlreadyRunningError("Network is already running")
        try:
            self._do_start()
            self._running = True
        except BaseException:
            self._do_stop()
            raise

    def stop(self) -> None:
        """Tear the network down. Idempotent — safe to call from any state."""
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

    def pingall6(
        self,
        *,
        count: int = 1,
        timeout: float = 2.0,
    ) -> dict[tuple[str, str], bool]:
        """IPv6 equivalent of pingall over hosts that have ``primary_ip6`` set.

        Hosts without a primary IPv6 are skipped silently.
        """
        eligible = {name: rh for name, rh in self._running_hosts.items() if rh.primary_ip6}
        result: dict[tuple[str, str], bool] = {}
        for src_name, src_host in eligible.items():
            for dst_name, dst_host in eligible.items():
                if src_name == dst_name:
                    continue
                target_ip6 = dst_host.primary_ip6
                assert target_ip6 is not None  # guarded by `eligible` filter
                result[(src_name, dst_name)] = src_host.ping(
                    target_ip6,
                    count=count,
                    timeout=timeout,
                    force_ipv6=True,
                )
        return result

    # ----- xterm helper -------------------------------------------------

    def xterm(
        self,
        host: str | RunningHost,
        *,
        title: str | None = None,
        shell: str = "bash",
    ) -> NSProcess:
        """Spawn an ``xterm`` running ``shell`` inside ``host``'s namespace.

        Returns the :class:`NSProcess`; the orchestrator tracks it and
        terminates it on :meth:`stop`. Raises :class:`NetworkError` if
        ``$DISPLAY`` is unset (no X server reachable from the current
        process). This method is intended for interactive use; the test
        suite does not exercise it because CI has no X server.
        """
        target = host if isinstance(host, RunningHost) else self.host(host)
        if not os.environ.get("DISPLAY"):
            raise NetworkError(
                "cannot spawn xterm: $DISPLAY is unset (no X server reachable). "
                "Set DISPLAY (e.g. ':0') and ensure xhost permits this process."
            )
        argv = ["xterm", "-T", title or f"p4net: {target.name}", "-e", shell]
        proc = target.popen(argv)
        self._spawned_processes.append(proc)
        return proc

    # ----- Internal start/stop ------------------------------------------

    def _do_start(self) -> None:
        logger.info(
            "Network.start: %d hosts, %d switches, %d links",
            len(self._topology.hosts),
            len(self._topology.switches),
            len(self._topology.links),
        )
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
            self._compile_results[sw_name] = self._compiler.compile(
                sw.p4_src,
                arch=sw.arch,
                extra_args=self._extra_compile_args,
            )

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
            self._host_iface_ip6[h_name] = {}
        for link in self._topology.links:
            self._wire_link(link, first_link_seen)

        # 8. default routes for hosts
        for h_name, host in self._topology.hosts.items():
            if host.default_route:
                self._namespaces[h_name].exec(
                    ["ip", "route", "add", "default", "via", host.default_route]
                )
            if host.default_route6:
                self._namespaces[h_name].exec(
                    ["ip", "-6", "route", "add", "default", "via", host.default_route6]
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
            logger.info("BMv2 %r ready on %s", sw_name, bmv2.grpc_address)

        # 10. P4Runtime clients
        election_id = (int(time.time_ns() // 1_000_000), 0)
        for sw_name, bmv2 in self._bmv2_switches.items():
            sw = self._topology.switches[sw_name]
            client = P4RuntimeClient(
                bmv2.grpc_address,
                device_id=int(sw.device_id) if sw.device_id is not None else 0,
                election_id=election_id,
                thrift_address=("127.0.0.1", int(bmv2.thrift_port)),
            )
            client.connect()
            self._clients[sw_name] = client
            client.set_pipeline_config(
                bmv2_json=self._compile_results[sw_name].bmv2_json,
                p4info=self._compile_results[sw_name].p4info,
            )
            logger.info("P4Runtime client connected to %r as primary", sw_name)

        # 11. RunningHost / RunningSwitch facades
        for h_name, host in self._topology.hosts.items():
            self._running_hosts[h_name] = RunningHost(
                host,
                self._namespaces[h_name],
                self._host_iface_ip[h_name],
                self._host_iface_ip6[h_name],
            )
        for sw_name, sw in self._topology.switches.items():
            self._running_switches[sw_name] = RunningSwitch(
                sw,
                self._bmv2_switches[sw_name],
                self._clients[sw_name],
                self._compile_results[sw_name],
            )
        logger.info("Network.start: ready")

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
        # Host-side configuration goes through `ip` inside the host namespace
        # (avoids per-side pyroute2.NetNS churn that surfaces flakiness when
        # several veth pairs are wired in rapid succession). Switch-side
        # configuration stays on VethPair's root-ns netlink path.
        for side, ep in (("a", link.a), ("b", link.b)):
            node = self._topology.node(ep.node)
            assert ep.iface_name is not None
            ip_to_use: str | None = None
            ip6_to_use: str | None = None
            mac_to_use: str | None = None
            if isinstance(node, Host):
                if ep.ip is not None:
                    ip_to_use = ep.ip
                elif node.ip is not None and node.name not in first_link_seen:
                    ip_to_use = node.ip
                if ep.ip6 is not None:
                    ip6_to_use = ep.ip6
                elif node.ip6 is not None and node.name not in first_link_seen:
                    ip6_to_use = node.ip6
                if ep.mac is not None:
                    mac_to_use = ep.mac
                elif node.mac is not None and node.name not in first_link_seen:
                    mac_to_use = node.mac
                first_link_seen.add(node.name)
                self._host_iface_ip[node.name][ep.iface_name] = ip_to_use
                self._host_iface_ip6[node.name][ep.iface_name] = ip6_to_use
                # Gate IPv6 BEFORE bringing the iface up so the kernel does not
                # auto-configure a link-local address we don't want.
                ns = self._namespaces[node.name]
                if ip6_to_use is not None:
                    enable_ipv6(ns, ep.iface_name)
                else:
                    disable_ipv6(ns, ep.iface_name)
                self._configure_host_iface(
                    ns,
                    ep.iface_name,
                    ip=ip_to_use,
                    ip6=ip6_to_use,
                    mac=mac_to_use,
                    mtu=link.mtu,
                )
            else:
                # Switch endpoint lives in the root namespace. Suppress its
                # IPv6 link-local so MLD chatter doesn't leak into PCAPs or
                # the CPU-punt stream.
                disable_ipv6(None, ep.iface_name)
                if ep.mac is not None:
                    mac_to_use = ep.mac
                if mac_to_use is not None:
                    veth.set_mac(side, mac_to_use)  # type: ignore[arg-type]
                if link.mtu is not None:
                    veth.set_mtu(side, link.mtu)  # type: ignore[arg-type]
                veth.set_up(side)  # type: ignore[arg-type]

        # Apply per-direction netem. The veth side at endpoint a shapes the
        # a→b direction (egress at a == arrives at b); same for b→a at b.
        a_rate, a_delay, a_jitter, a_loss = self._direction_params(link, "a_to_b")
        if any(v is not None for v in (a_rate, a_delay, a_jitter, a_loss)):
            node_a = self._topology.node(link.a.node)
            ns_a = self._namespaces[node_a.name] if isinstance(node_a, Host) else None
            assert link.a.iface_name is not None
            apply_netem(
                ns_a,
                link.a.iface_name,
                rate=a_rate,
                delay=a_delay,
                jitter=a_jitter,
                loss_pct=a_loss,
            )
        b_rate, b_delay, b_jitter, b_loss = self._direction_params(link, "b_to_a")
        if any(v is not None for v in (b_rate, b_delay, b_jitter, b_loss)):
            node_b = self._topology.node(link.b.node)
            ns_b = self._namespaces[node_b.name] if isinstance(node_b, Host) else None
            assert link.b.iface_name is not None
            apply_netem(
                ns_b,
                link.b.iface_name,
                rate=b_rate,
                delay=b_delay,
                jitter=b_jitter,
                loss_pct=b_loss,
            )

    @staticmethod
    def _direction_params(
        link: Link,
        direction: str,
    ) -> tuple[str | None, str | None, str | None, float | None]:
        """Return ``(rate, delay, jitter, loss_pct)`` netem args for one direction.

        Each element falls back to the symmetric value if the matching
        ``*_a_to_b`` / ``*_b_to_a`` field is unset. If the matching
        ``*_extra`` field is set, it is summed on top of the symmetric base.
        """
        suffix = "_a_to_b" if direction == "a_to_b" else "_b_to_a"
        rate: str | None = getattr(link, "bandwidth" + suffix) or link.bandwidth
        delay = _resolve_dir_str(
            link.delay, getattr(link, "delay" + suffix), getattr(link, "delay" + suffix + "_extra")
        )
        jitter = _resolve_dir_str(
            link.jitter,
            getattr(link, "jitter" + suffix),
            getattr(link, "jitter" + suffix + "_extra"),
        )
        loss = _resolve_dir_loss(
            link.loss_pct,
            getattr(link, "loss_pct" + suffix),
            getattr(link, "loss_pct" + suffix + "_extra"),
        )
        return rate, delay, jitter, loss

    @staticmethod
    def _configure_host_iface(
        ns: NetworkNamespace,
        iface: str,
        *,
        ip: str | None,
        ip6: str | None,
        mac: str | None,
        mtu: int | None,
    ) -> None:
        if mac is not None:
            ns.exec(["ip", "link", "set", iface, "address", mac])
        if mtu is not None:
            ns.exec(["ip", "link", "set", iface, "mtu", str(mtu)])
        if ip is not None:
            ns.exec(["ip", "addr", "add", ip, "dev", iface])
        if ip6 is not None:
            ns.exec(["ip", "-6", "addr", "add", ip6, "dev", iface])
        ns.exec(["ip", "link", "set", iface, "up"])

    def _port_to_iface_for(self, switch_name: str) -> dict[int, str]:
        result: dict[int, str] = {}
        for link in self._topology.links:
            for ep in (link.a, link.b):
                if ep.node == switch_name and ep.port is not None and ep.iface_name is not None:
                    result[int(ep.port)] = ep.iface_name
        return result

    def _do_stop(self) -> None:
        if self._running:
            logger.info("Network.stop: tearing down")
        # 0. user-spawned processes (xterm, etc.) — reap before namespaces vanish.
        for proc in list(self._spawned_processes):
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        proc.kill()
            except Exception as exc:
                logger.warning("spawned process reap (pid=%s): %r", getattr(proc, "pid", "?"), exc)
        self._spawned_processes.clear()

        # 1a. Async P4Runtime clients (if any were lazily constructed and
        # connected). We run a fresh event loop per client to avoid
        # interfering with the caller's loop; a failure here is logged
        # and does not block shutdown.
        for sw_name, rs in list(self._running_switches.items()):
            ac = rs._async_client
            if ac is None:
                continue
            try:
                if ac.is_connected:
                    import asyncio as _asyncio

                    _asyncio.run(ac.disconnect())
            except Exception as exc:
                logger.warning("async client disconnect %r: %r", sw_name, exc)
            rs._reset_async_client()

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
        self._host_iface_ip6.clear()
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
