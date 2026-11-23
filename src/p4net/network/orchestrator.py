"""End-to-end topology orchestrator.

`Network.start()` brings a `Topology` up step by step (compile P4, create
namespaces, wire veth pairs, address them, apply impairments, launch BMv2,
connect P4Runtime, push pipelines). Any failure rolls back partial state
via `_do_stop()` before re-raising. `Network.stop()` is fully idempotent.

Cleanup safety net: the `_cleanup` module installs an atexit hook plus
signal handlers on the main thread. In an event loop, signal
cleanup awaits `astop()` before re-delivering the signal.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from itertools import permutations
from pathlib import Path
from types import TracebackType

from p4net.compiler import CompileResult, P4Compiler
from p4net.control import AsyncP4RuntimeClient, ConnectionError, P4RuntimeClient
from p4net.network._cleanup import (
    defer_cleanup_signals,
    install_handlers,
    register,
    unregister,
    wait_for_signal_cleanup,
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
from p4net.topo._addresses import _effective_host_addresses
from p4net.topo.exceptions import TopologyError

logger = logging.getLogger(__name__)

_IPV6_READY_TIMEOUT = 5.0
_XTERM_STOP_TIMEOUT = 2.0

_DURATION_UNITS_NS: dict[str, int] = {
    "ns": 1,
    "us": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
}


@dataclass
class _AsyncDisconnect:
    switch: str
    client: AsyncP4RuntimeClient
    loop: asyncio.AbstractEventLoop
    future: Future[None] | None = None

    def done(self) -> bool:
        # A finished future can still leave the client owning an open channel.
        return (self.future is None or self.future.done()) and self.client._owning_loop is None


@dataclass
class _ShutdownFailures:
    channels: dict[str, ConnectionError]
    switches: dict[str, Exception]
    veth_pairs: dict[VethPair, Exception]
    namespaces: dict[str, Exception]
    host_commands: dict[int, Exception]
    terminals: dict[int, Exception]


def _hosts_with_ip(hosts: Mapping[str, RunningHost], *, ipv6: bool) -> dict[str, RunningHost]:
    return {
        name: host
        for name, host in hosts.items()
        if (host.primary_ip6 if ipv6 else host.primary_ip)
    }


def _ping_host_pairs(
    hosts: Mapping[str, RunningHost], *, count: int, timeout: float, ipv6: bool
) -> Iterator[tuple[tuple[str, str], bool]]:
    for (src_name, src_host), (dst_name, dst_host) in permutations(hosts.items(), 2):
        if ipv6:
            target = dst_host.primary_ip6
            assert target is not None  # Hosts are selected by _hosts_with_ip.
            ok = src_host.ping(target, count=count, timeout=timeout, force_ipv6=True)
        else:
            ok = src_host.ping(dst_host, count=count, timeout=timeout)
        yield (src_name, dst_name), ok


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
    """Resolve directional loss, rejecting symmetric-plus-extra totals above 100."""
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
        self._host_commands: set[NSProcess] = set()
        self._pending_async_retirements: dict[str, RunningSwitch] = {}
        self._async_disconnects: list[_AsyncDisconnect] = []
        self._closed_async_client_loops: set[str] = set()
        self._stop_lock = threading.RLock()
        self._start_future: Future[None] | None = None
        self._start_thread_id: int | None = None
        self._stop_future: Future[None] | None = None
        self._stop_thread_id: int | None = None

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
        """Map each running switch's name to its ``boot_timestamp_us``.

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
        P4Runtime clients, and pushes pipeline configs. Waits up to five
        seconds for configured IPv6 addresses to finish duplicate address
        detection. On failure, rolls back via :meth:`_do_stop` before re-raising.

        Raises:
            NetworkAlreadyRunningError: if already running or starting.
            NetworkError: if shutdown is in progress or interrupts startup,
                or configured IPv6 addresses fail duplicate address detection
                or remain unready.
        """
        self._run_start(self._prepare_start())

    def _prepare_start(self, *, background: bool = False) -> Future[None]:
        with self._stop_lock:
            if self._running or self._start_future is not None:
                raise NetworkAlreadyRunningError("Network is already running or starting")
            stopping = self._stop_future is not None and not self._stop_future.done()
            if (
                stopping
                or self._bmv2_switches
                or self._veth_pairs
                or self._namespaces
                or self._host_commands
                or self._spawned_processes
                or any(
                    client._has_channel or not client._retired for client in self._clients.values()
                )
                or self._pending_async_retirements
                or any(not close.done() for close in self._async_disconnects)
            ):
                raise NetworkError("Network shutdown is still running")
            self._clients.clear()
            self._async_disconnects.clear()
            self._closed_async_client_loops.clear()
            self._stop_future = None
            self._start_thread_id = None if background else threading.get_ident()
            self._start_future = Future()
            return self._start_future

    def _end_start(self, future: Future[None]) -> None:
        with self._stop_lock:
            future.set_result(None)
            self._start_future = None
            self._start_thread_id = None

    def _run_start(self, future: Future[None]) -> None:
        with self._stop_lock:
            self._start_thread_id = threading.get_ident()
        try:
            with self._stop_lock:
                if self._stop_future is not None:
                    raise NetworkError("Network startup was interrupted by shutdown")
            self._do_start()
            with self._stop_lock:
                if self._stop_future is not None:
                    raise NetworkError("Network startup was interrupted by shutdown")
                self._running = True
        except BaseException:
            self._rollback_startup()
            raise
        finally:
            self._end_start(future)

    def _rollback_startup(self) -> None:
        try:
            self._do_stop()
        except Exception:
            logger.exception("Network startup rollback failed")

    def _register_cleanup(self) -> None:
        if not self._registered or threading.current_thread() is threading.main_thread():
            install_handlers()
        if not self._registered:
            register(self)
            self._registered = True

    async def astart(self) -> None:
        """Bring the topology up in a worker thread without blocking the event loop.

        Call from the main thread's event loop to install signal cleanup
        handlers. Cancellation waits for startup and attempts shutdown before
        propagating. A shutdown error becomes the cause of the cancellation.
        Startup failures roll back partial resources and propagate their
        original exception.

        Raises:
            NetworkAlreadyRunningError: if already running or starting.
            NetworkError: if shutdown is in progress or interrupts startup.
        """
        future = self._prepare_start(background=True)
        try:
            self._register_cleanup()
            startup = asyncio.get_running_loop().run_in_executor(None, self._run_start, future)
        except BaseException:
            try:
                self._rollback_startup()
            finally:
                self._end_start(future)
            raise

        cancellation: asyncio.CancelledError | None = None
        try:
            while not startup.done():
                try:
                    await asyncio.shield(startup)
                except asyncio.CancelledError as exc:
                    if cancellation is None:
                        cancellation = exc
            startup.result()
        except BaseException:
            await wait_for_signal_cleanup()
            if cancellation is None or self._stop_future is None:
                raise
        if cancellation is not None:
            try:
                await self.astop()
            except asyncio.CancelledError as exc:
                # astop has already waited for cleanup and chained any failure.
                raise cancellation from exc.__cause__
            except Exception as exc:
                raise cancellation from exc
            raise cancellation from None

    def stop(self) -> None:
        """Tear the network down. Repeated calls are safe.

        Async clients close on their owning event loops. Inside an event
        loop, shutdown runs in a background thread; use :meth:`astop` to
        wait for it. Outside an event loop, this method waits for cleanup.
        Startup already in progress finishes or rolls back before teardown.

        Raises:
            NetworkError: if host process, BMv2, veth, or namespace cleanup
                fails, a gRPC channel close fails or times out, or an async
                client's event loop is paused or closed. Call again to retry
                pending cleanup.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            in_event_loop = False
        else:
            in_event_loop = True

        with self._stop_lock:
            if self._stop_future is None or (
                self._stop_future.done() and self._has_pending_cleanup()
            ):
                self._stop_future = Future()
                initiate = True
            else:
                initiate = False
            future = self._stop_future
        if initiate:
            if in_event_loop:
                threading.Thread(
                    target=self._finish_stop, args=(future,), name="p4net-stop", daemon=True
                ).start()
            else:
                self._finish_stop(future)
        if not in_event_loop and threading.get_ident() not in (
            self._stop_thread_id,
            self._start_thread_id,
        ):
            future.result()

    def _finish_stop(self, future: Future[None]) -> None:
        self._stop_thread_id = threading.get_ident()
        error: BaseException | None = None
        try:
            with self._stop_lock:
                startup = self._start_future
                start_thread_id = self._start_thread_id
            if startup is not None and start_thread_id != threading.get_ident():
                startup.result()
            failures = self._do_stop()
            if failures.switches:
                switches = ", ".join(repr(name) for name in sorted(failures.switches))
                error = NetworkError(f"shutdown failed: could not stop BMv2 process for {switches}")
                error.__cause__ = next(iter(failures.switches.values()))
            elif failures.veth_pairs:
                pairs = ", ".join(repr(pair) for pair in failures.veth_pairs)
                error = NetworkError(f"shutdown failed: could not destroy veth pairs {pairs}")
                error.__cause__ = next(iter(failures.veth_pairs.values()))
            elif failures.namespaces:
                namespaces = ", ".join(repr(name) for name in sorted(failures.namespaces))
                error = NetworkError(
                    f"shutdown failed: could not destroy host namespaces {namespaces}"
                )
                error.__cause__ = next(iter(failures.namespaces.values()))
            elif failures.host_commands:
                pids = sorted(failures.host_commands)
                error = NetworkError(
                    "shutdown failed: could not stop host command processes "
                    f"{', '.join(map(str, pids))}"
                )
                error.__cause__ = failures.host_commands[pids[0]]
            elif failures.terminals:
                pids = sorted(failures.terminals)
                error = NetworkError(
                    f"shutdown failed: could not stop xterm processes {', '.join(map(str, pids))}"
                )
                error.__cause__ = failures.terminals[pids[0]]
            elif self._closed_async_client_loops:
                switches = ", ".join(repr(name) for name in sorted(self._closed_async_client_loops))
                error = NetworkError(
                    f"async client shutdown failed: event loop is closed for {switches}"
                )
            elif failures.channels:
                switches = ", ".join(repr(name) for name in sorted(failures.channels))
                error = NetworkError(
                    f"client shutdown failed: could not close gRPC channel for {switches}"
                )
                error.__cause__ = next(iter(failures.channels.values()))
            elif self._async_disconnects:
                error = NetworkError("async client shutdown did not finish")
        except BaseException as exc:
            error = exc
        finally:
            self._stop_thread_id = None
        if error is None:
            future.set_result(None)
        else:
            future.set_exception(error)

    async def astop(self) -> None:
        """Wait for async clients and network resources to finish shutting down.

        Client event loops must remain running until shutdown completes.
        Cancellation of this call is propagated after the cleanup attempt.
        If cleanup fails, the cancellation carries the failure as its cause.

        Raises:
            NetworkError: if host process, BMv2, veth, or namespace cleanup
                fails, a gRPC channel close fails or times out, or an async
                client's event loop is paused or closed. Call again to retry
                pending cleanup.
        """
        with self._stop_lock:
            self.stop()
            future = self._stop_future
        assert future is not None
        shutdown = asyncio.wrap_future(future)
        cancellation: asyncio.CancelledError | None = None
        try:
            while not shutdown.done():
                try:
                    await asyncio.shield(shutdown)
                except asyncio.CancelledError as exc:
                    if cancellation is None:
                        cancellation = exc
            shutdown.result()
        except Exception as exc:
            if cancellation is not None:
                raise cancellation from exc
            raise
        if cancellation is not None:
            raise cancellation

    # Ping helpers -------------------------------------------------------

    def ping(
        self,
        src: str | RunningHost,
        dst: str | RunningHost,
        *,
        count: int = 1,
        timeout: float = 2.0,
    ) -> bool:
        """Send at most `count` echo requests from `src` to `dst`.

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
        hosts = _hosts_with_ip(self._running_hosts, ipv6=False)
        return dict(_ping_host_pairs(hosts, count=count, timeout=timeout, ipv6=False))

    def pingall6(
        self,
        *,
        count: int = 1,
        timeout: float = 2.0,
    ) -> dict[tuple[str, str], bool]:
        """IPv6 equivalent of pingall over hosts that have ``primary_ip6`` set.

        Hosts without a primary IPv6 are skipped silently.
        """
        hosts = _hosts_with_ip(self._running_hosts, ipv6=True)
        return dict(_ping_host_pairs(hosts, count=count, timeout=timeout, ipv6=True))

    # ----- Host commands ------------------------------------------------

    @contextlib.contextmanager
    def _host_command(self, host: RunningHost, argv: Sequence[str]) -> Iterator[NSProcess]:
        """Track the command group until the interactive caller finishes with it."""
        process: NSProcess | None = None
        try:
            with defer_cleanup_signals(), self._stop_lock:
                if self._stop_future is not None:
                    raise NetworkError("Network is shutting down or has stopped")
                process = host.namespace.popen(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                )
                self._host_commands.add(process)
            yield process
        except BaseException:
            if process is not None:
                with defer_cleanup_signals(), self._stop_lock:
                    # The shutdown worker owns these handles once stop() starts.
                    if self._stop_future is None and process in self._host_commands:
                        try:
                            process._stop_group()
                        except Exception as exc:
                            logger.warning("host command cleanup (pid=%s): %r", process.pid, exc)
                        else:
                            self._host_commands.discard(process)
            raise
        else:
            with self._stop_lock:
                if self._stop_future is None:
                    self._host_commands.discard(process)
        finally:
            if process is not None:
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()

    def xterm(
        self,
        host: str | RunningHost,
        *,
        title: str | None = None,
        shell: str = "bash",
    ) -> NSProcess:
        """Spawn an ``xterm`` running ``shell`` inside ``host``'s namespace.

        Returns an :class:`NSProcess` that :meth:`stop` terminates and waits
        for. Raises :class:`NetworkError` if ``$DISPLAY`` is unset or the
        network is shutting down or has stopped.
        """
        with defer_cleanup_signals(), self._stop_lock:
            if self._stop_future is not None:
                raise NetworkError("Network is shutting down or has stopped")
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
        self._register_cleanup()

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
                pcap_dir=self._pcap_dir if sw.pcap_enabled else None,
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
            self._clients[sw_name] = client
            client.connect()
            client.set_pipeline_config(
                bmv2_json=self._compile_results[sw_name].bmv2_json,
                p4info=self._compile_results[sw_name].p4info,
            )
            logger.info("P4Runtime client connected to %r as primary", sw_name)

        # 11. Configured IPv6 addresses must be usable before startup succeeds.
        self._wait_for_ipv6_addresses()

        # 12. RunningHost / RunningSwitch facades
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

    def _wait_for_ipv6_addresses(self) -> None:
        pending = {
            (host, iface): ipaddress.IPv6Interface(cidr).ip
            for host, interfaces in self._host_iface_ip6.items()
            for iface, cidr in interfaces.items()
            if cidr is not None
        }
        if not pending:
            return
        timeout = _IPV6_READY_TIMEOUT
        deadline = time.monotonic() + timeout

        def not_ready() -> NetworkError:
            addresses = ", ".join(
                f"{host}/{iface} ({address})" for (host, iface), address in pending.items()
            )
            return NetworkError(f"IPv6 addresses not ready after {timeout:g}s: {addresses}")

        while pending:
            with self._stop_lock:
                if self._stop_future is not None:
                    raise NetworkError("Network startup was interrupted by shutdown")
            for host in dict.fromkeys(name for name, _ in pending):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise not_ready()
                try:
                    result = self._namespaces[host].exec(
                        ["ip", "-j", "-6", "addr", "show"],
                        capture_output=True,
                        timeout=remaining,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise not_ready() from exc
                for interface in json.loads(result.stdout):
                    key = (host, interface["ifname"])
                    if key not in pending:
                        continue
                    for address in interface.get("addr_info", []):
                        if ipaddress.IPv6Address(address["local"]) != pending[key]:
                            continue
                        if address.get("dadfailed", False):
                            raise NetworkError(
                                f"IPv6 address {pending[key]} on {host}/{key[1]} "
                                "failed duplicate address detection"
                            )
                        if not address.get("tentative", False):
                            del pending[key]
                        break
            if pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise not_ready()
                time.sleep(min(0.05, remaining))

    def _wire_link(self, link: Link, first_link_seen: set[str]) -> None:
        name_a = link.a.iface_name
        name_b = link.b.iface_name
        assert name_a is not None and name_b is not None, (
            "link endpoints must have iface_name resolved by Topology"
        )
        node_a = self._topology.node(link.a.node)
        node_b = self._topology.node(link.b.node)
        veth = VethPair(
            name_a,
            name_b,
            namespace_a=self._namespaces[node_a.name] if isinstance(node_a, Host) else None,
            namespace_b=self._namespaces[node_b.name] if isinstance(node_b, Host) else None,
        )
        veth.create()
        self._veth_pairs.append(veth)

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
                ip_to_use, ip6_to_use = _effective_host_addresses(
                    node, ep, first_link=node.name not in first_link_seen
                )
                if ep.mac is not None:
                    mac_to_use = ep.mac
                elif node.mac is not None and node.name not in first_link_seen:
                    mac_to_use = node.mac
                first_link_seen.add(node.name)
                self._host_iface_ip[node.name][ep.iface_name] = ip_to_use
                self._host_iface_ip6[node.name][ep.iface_name] = ip6_to_use
                # Configure IPv6 before bringing up the host interface.
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

    def _settle_closed_async_loop(self, close: _AsyncDisconnect) -> bool:
        """Settle a disconnect whose owning loop can no longer run."""
        if not close.loop.is_closed():
            return False
        if close.client._owning_loop is not None:
            self._closed_async_client_loops.add(close.switch)
            logger.warning("async client disconnect %r failed: event loop is closed", close.switch)
        return True

    def _close_async_clients(self) -> dict[str, ConnectionError]:
        failed_channels: dict[str, ConnectionError] = {}
        pending = []
        scheduled = []
        for close in self._async_disconnects:
            if self._settle_closed_async_loop(close):
                continue
            if close.future is not None:
                scheduled.append(close)
                continue
            if close.done():
                continue
            loop = close.loop
            if not loop.is_running() and not loop.is_closed():
                pending.append(close)
                logger.warning(
                    "async client disconnect %r deferred: event loop is paused", close.switch
                )
                continue
            try:
                disconnect = close.client.disconnect()
                try:
                    close.future = asyncio.run_coroutine_threadsafe(disconnect, loop)
                except BaseException:
                    disconnect.close()
                    raise
                scheduled.append(close)
            except Exception as exc:
                if self._settle_closed_async_loop(close) or close.done():
                    continue
                failure = ConnectionError(
                    f"Could not schedule async gRPC disconnect for {close.client.target!r}"
                )
                failure.__cause__ = exc
                pending.append(close)
                failed_channels[close.switch] = failure
                logger.warning("could not schedule async disconnect %r: %r", close.switch, exc)

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        for close in scheduled:
            if self._settle_closed_async_loop(close):
                continue
            future = close.future
            assert future is not None
            if close.loop is current_loop and not future.done():
                pending.append(close)
                continue
            try:
                # Read the task's exception separately from a timeout waiting for it.
                error = future.exception(timeout=5.0)
            except FutureTimeoutError:
                if not self._settle_closed_async_loop(close):
                    pending.append(close)
                    logger.warning("async client disconnect %r timed out", close.switch)
                continue
            except FutureCancelledError as exc:
                error = exc
            if error is None:
                continue
            if not isinstance(error, Exception):
                raise error
            if self._settle_closed_async_loop(close):
                continue
            if isinstance(error, ConnectionError):
                failure = error
            elif close.done():
                if not isinstance(error, FutureCancelledError):
                    logger.warning("async client disconnect %r: %r", close.switch, error)
                continue
            else:
                reason = (
                    "was cancelled"
                    if isinstance(error, FutureCancelledError)
                    else f"failed with {error!r}"
                )
                failure = ConnectionError(f"Async disconnect for {close.client.target!r} {reason}")
                failure.__cause__ = error
            close.future = None
            pending.append(close)
            failed_channels[close.switch] = failure
            logger.warning("async client disconnect %r: %s", close.switch, failure)
        self._async_disconnects = pending
        return failed_channels

    def _has_pending_cleanup(self) -> bool:
        return bool(
            self._clients
            or self._pending_async_retirements
            or self._async_disconnects
            or self._bmv2_switches
            or self._veth_pairs
            or self._namespaces
            or self._host_commands
            or self._spawned_processes
        )

    def _do_stop(self) -> _ShutdownFailures:
        if self._running:
            logger.info("Network.stop: tearing down")
        # 0. user-spawned processes (xterm, etc.) — reap before namespaces vanish.
        failed_host_commands: dict[int, Exception] = {}
        with self._stop_lock:
            commands = tuple(self._host_commands)
            terminals = tuple(self._spawned_processes)
        for command in commands:
            try:
                command._stop_group()
            except Exception as exc:
                failed_host_commands[command.pid] = exc
                logger.warning("host command cleanup (pid=%s): %r", command.pid, exc)
            else:
                with self._stop_lock:
                    self._host_commands.discard(command)

        failed_terminals: dict[int, Exception] = {}
        pending_terminals: list[NSProcess] = []
        for proc in terminals:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=_XTERM_STOP_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=_XTERM_STOP_TIMEOUT)
            except Exception as exc:
                failed_terminals[proc.pid] = exc
                pending_terminals.append(proc)
                logger.warning("xterm cleanup (pid=%s): %r", proc.pid, exc)
        with self._stop_lock:
            self._spawned_processes = pending_terminals

        # Reject sync reconnects while waiting for async clients to close.
        pending_clients: dict[str, P4RuntimeClient] = {}
        failed_channels: dict[str, ConnectionError] = {}
        for sw_name, client in self._clients.items():
            try:
                client._retire()
            except Exception as exc:
                failure = ConnectionError(
                    f"Could not retire P4Runtime client for {client.target!r}: {exc!r}"
                )
                failure.__cause__ = exc
                pending_clients[sw_name] = client
                failed_channels[sw_name] = failure
                logger.warning("retire client %r: %s", sw_name, failure)

        # 1a. Close async clients on the loops that own their gRPC resources.
        pending_async_retirements: dict[str, RunningSwitch] = {}
        for sw_name, rs in (self._pending_async_retirements | self._running_switches).items():
            try:
                cleanup = rs._take_async_client_for_stop()
            except Exception as exc:
                failure = ConnectionError(f"Could not retire async P4Runtime client: {exc!r}")
                failure.__cause__ = exc
                pending_async_retirements[sw_name] = rs
                failed_channels[sw_name] = failure
                logger.warning("retire async client %r: %s", sw_name, failure)
                continue
            if cleanup is None:
                continue
            ac, loop = cleanup
            if loop is not None:
                self._async_disconnects.append(_AsyncDisconnect(sw_name, ac, loop))
        self._pending_async_retirements = pending_async_retirements
        failed_channels.update(self._close_async_clients())

        # 1. P4Runtime clients
        for sw_name, client in list(self._clients.items()):
            if sw_name in pending_clients:
                continue
            try:
                client.disconnect()
            except Exception as exc:
                if isinstance(exc, ConnectionError):
                    failure = exc
                elif client._has_channel:
                    failure = ConnectionError(
                        f"Could not close gRPC channel for {client.target!r}: {exc!r}"
                    )
                    failure.__cause__ = exc
                else:
                    logger.warning("disconnect %r: %r", sw_name, exc)
                    continue
                pending_clients[sw_name] = client
                failed_channels.setdefault(sw_name, failure)
                logger.warning("disconnect %r: %s", sw_name, failure)
        self._clients = pending_clients

        # 2. BMv2 switches
        pending_switches = {}
        failed_switches: dict[str, Exception] = {}
        for sw_name, bmv2 in list(self._bmv2_switches.items()):
            try:
                bmv2.stop()
                if bmv2.is_running():
                    raise RuntimeError(f"BMv2 process for {sw_name!r} is still running")
            except Exception as exc:
                pending_switches[sw_name] = bmv2
                failed_switches[sw_name] = exc
                logger.warning("BMv2 stop %r: %r", sw_name, exc)
        self._bmv2_switches = pending_switches

        # 3. veth pairs
        failed_veth_pairs: dict[VethPair, Exception] = {}
        for veth in list(self._veth_pairs):
            try:
                veth.destroy()
            except Exception as exc:
                failed_veth_pairs[veth] = exc
                logger.warning("veth destroy %r: %r", veth, exc)
        self._veth_pairs = list(failed_veth_pairs)

        # 4. namespaces
        link_namespaces = {
            namespace
            for veth in self._veth_pairs
            for namespace in (veth.namespace_of("a"), veth.namespace_of("b"))
            if namespace is not None
        }
        pending_namespaces = {}
        failed_namespaces: dict[str, Exception] = {}
        for h_name, ns in list(self._namespaces.items()):
            if ns in link_namespaces:
                pending_namespaces[h_name] = ns
                continue
            try:
                if ns.exists:
                    ns.destroy()
            except Exception as exc:
                pending_namespaces[h_name] = ns
                failed_namespaces[h_name] = exc
                logger.warning("ns destroy %r: %r", h_name, exc)
        self._namespaces = pending_namespaces

        # 5. unregister hooks
        if self._registered and not self._has_pending_cleanup():
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
        return _ShutdownFailures(
            failed_channels,
            failed_switches,
            failed_veth_pairs,
            failed_namespaces,
            failed_host_commands,
            failed_terminals,
        )

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
