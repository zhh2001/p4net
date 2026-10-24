"""Process lifecycle wrapper for `simple_switch_grpc`.

Architectural choices baked into this module:

1. **Root namespace**, not per-switch namespaces. Each BMv2 process runs in
   the host's root network namespace; only host endpoints get their own
   `NetworkNamespace`. This is the Mininet pattern and avoids
   gRPC-port-reachability headaches that per-switch namespaces would create.
   We therefore use `subprocess.Popen` directly here, NOT `NSProcess`.
2. **The wrapper does NOT create veth interfaces.** Every interface name in
   `port_to_iface` must already exist in the root namespace by the time
   `start()` is called; phase 6 wires veths up before instantiating
   `BMv2Switch`. We do not validate interface existence; the kernel will.
3. **The wrapper does NOT push P4Runtime config.** Phase 5 wires up the
   P4Runtime client. The `bmv2_json` argument is loaded by
   `simple_switch_grpc` at startup, so the data plane has a pipeline
   immediately even before any gRPC client connects.

Invocation: built and verified against `simple_switch_grpc 1.15`. The full
command line is:
``simple_switch_grpc --device-id N -i 1@iface1 ... --thrift-port T
--log-console --log-flush -L <level> [--pcap <dir>] <bmv2_json>
-- --grpc-server-addr <host>:<port> [--cpu-port C]``.
Flags before the ``--`` go to the BMv2 simple_switch core (and that's where
the JSON config path lives, per the binary's own usage string ``[options]
<path to JSON config file>``); flags after ``--`` are simple_switch_grpc
target-specific.
"""

from __future__ import annotations

import logging
import math
import shutil
import socket
import subprocess
import time
from collections.abc import Mapping
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from types import TracebackType

from pyroute2 import DiagSocket
from pyroute2.netlink.diag import SS_LISTEN
from pyroute2.netlink.exceptions import NetlinkError

from p4net.runtime.exceptions import BMv2NotFoundError, BMv2StartupError

logger = logging.getLogger(__name__)

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"trace", "debug", "info", "warn", "error"})
_READY_POLL_INTERVAL = 0.1


def _validate_timeout(value: float, name: str) -> None:
    try:
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite positive number")


def _listener_matches(
    host: str, addresses: set[IPv4Address | IPv6Address], *, ipv6_only: bool
) -> bool:
    listener = ip_address(host)
    if isinstance(listener, IPv6Address) and listener.ipv4_mapped is not None:
        listener = listener.ipv4_mapped
    for address in addresses:
        if listener == address:
            return True
        if listener.version == address.version:
            if listener.is_unspecified or address.is_unspecified:
                return True
        elif isinstance(listener, IPv6Address) and listener.is_unspecified and not ipv6_only:
            return True
    return False


class BMv2Switch:
    """Process lifecycle wrapper for a single `simple_switch_grpc` instance."""

    def __init__(
        self,
        name: str,
        *,
        device_id: int,
        grpc_port: int,
        thrift_port: int,
        bmv2_json: Path,
        port_to_iface: Mapping[int, str],
        log_dir: Path,
        pcap_dir: Path | None = None,
        cpu_port: int | None = None,
        log_level: str = "info",
        grpc_bind_addr: str = "127.0.0.1",
        binary: str = "simple_switch_grpc",
        startup_timeout: float = 10.0,
    ) -> None:
        if not name:
            raise ValueError("BMv2Switch name must be non-empty")
        if not isinstance(log_level, str) or log_level not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"invalid log_level {log_level!r}: must be one of {sorted(_VALID_LOG_LEVELS)}"
            )
        _validate_timeout(startup_timeout, "startup_timeout")
        self._name = name
        self._device_id = device_id
        self._grpc_port = grpc_port
        self._thrift_port = thrift_port
        self._bmv2_json = Path(bmv2_json)
        self._port_to_iface: dict[int, str] = dict(port_to_iface)
        self._log_dir = Path(log_dir)
        self._pcap_dir = Path(pcap_dir) if pcap_dir is not None else None
        self._cpu_port = cpu_port
        self._log_level = log_level
        self._grpc_bind_addr = grpc_bind_addr
        self._binary = binary
        self._startup_timeout = startup_timeout

        self._proc: subprocess.Popen[bytes] | None = None
        self._log_file_handle: object | None = None  # an open file, kept alive
        self._started = False
        self._boot_timestamp_us: int | None = None

    # Properties ---------------------------------------------------------

    @property
    def name(self) -> str:
        """The switch name (used as the basename of the log file)."""
        return self._name

    @property
    def pid(self) -> int | None:
        """OS process ID, or ``None`` if the switch has not been started."""
        return None if self._proc is None else self._proc.pid

    @property
    def grpc_address(self) -> str:
        """``host:port`` string the P4Runtime gRPC server binds to."""
        return f"{self._grpc_bind_addr}:{self._grpc_port}"

    @property
    def log_file(self) -> Path:
        """Path to the BMv2 log file inside ``log_dir``."""
        return self._log_dir / f"{self._name}.log"

    @property
    def device_id(self) -> int:
        """P4Runtime device ID this switch reports as."""
        return self._device_id

    @property
    def thrift_port(self) -> int:
        """Thrift bind port for ``simple_switch_CLI`` and register operations."""
        return self._thrift_port

    @property
    def grpc_port(self) -> int:
        """gRPC port the P4Runtime server bound to (host portion is ``grpc_bind_addr``)."""
        return self._grpc_port

    @property
    def boot_timestamp_us(self) -> int | None:
        """Wall-clock microseconds since Unix epoch when this BMv2 process started.

        ``None`` if the switch has not been started yet, or has been stopped
        since its last start. Captured immediately before ``subprocess.Popen``,
        so drift from BMv2's internal clock zero is bounded by Popen overhead
        (sub-millisecond on a typical Linux host).

        Combined with INT shim ``ingress_timestamp_us`` to derive wall-clock
        arrival time across multiple switches::

            wall_clock_us = bmv2.boot_timestamp_us + shim.ingress_timestamp_us
        """
        return self._boot_timestamp_us

    # Argv construction --------------------------------------------------

    def _build_argv(self) -> list[str]:
        argv: list[str] = [
            self._binary,
            "--device-id",
            str(self._device_id),
        ]
        for port in sorted(self._port_to_iface):
            argv += ["-i", f"{port}@{self._port_to_iface[port]}"]
        argv += [
            "--thrift-port",
            str(self._thrift_port),
            "--log-console",
            "--log-flush",
            "-L",
            self._log_level,
        ]
        if self._pcap_dir is not None:
            argv += ["--pcap", str(self._pcap_dir)]
        # The JSON config path is a positional argument of the BMv2 core
        # parser, so it must precede the `--` separator. Anything after `--`
        # is consumed by the simple_switch_grpc target parser.
        argv.append(str(self._bmv2_json))
        argv.append("--")
        argv += ["--grpc-server-addr", f"{self._grpc_bind_addr}:{self._grpc_port}"]
        if self._cpu_port is not None:
            argv += ["--cpu-port", str(self._cpu_port)]
        return argv

    # Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Spawn the simple_switch_grpc process. Non-blocking."""
        if self._started:
            raise BMv2StartupError(f"BMv2 {self._name!r} already started (pid={self.pid})")
        binary_path = shutil.which(self._binary)
        if binary_path is None:
            raise BMv2NotFoundError(f"binary {self._binary!r} not found on PATH")

        self._log_dir.mkdir(parents=True, exist_ok=True)
        if self._pcap_dir is not None:
            self._pcap_dir.mkdir(parents=True, exist_ok=True)

        argv = self._build_argv()
        # Replace argv[0] with the absolute path so PATH lookups don't
        # surprise us at exec time.
        argv[0] = binary_path
        log_path = self.log_file
        log_handle = log_path.open("ab")
        self._log_file_handle = log_handle
        logger.debug("BMv2 %r starting: %s", self._name, argv)
        # Capture wall-clock immediately before Popen — drift from BMv2's
        # internal clock zero is bounded by Popen overhead. Do NOT add any
        # work between this assignment and the Popen call.
        self._boot_timestamp_us = time.time_ns() // 1000
        try:
            self._proc = subprocess.Popen(
                argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                bufsize=0,
                close_fds=True,
            )
        except OSError as exc:
            log_handle.close()
            self._log_file_handle = None
            self._boot_timestamp_us = None
            raise BMv2StartupError(f"failed to spawn BMv2 {self._name!r}: {exc}") from exc
        self._started = True
        logger.debug("BMv2 %r started (pid=%d, log=%s)", self._name, self._proc.pid, log_path)

    def wait_until_ready(self, *, timeout: float | None = None) -> None:
        """Wait until this process owns its gRPC and Thrift listeners, or fail.

        A listener owned by another process, including a shared gRPC port,
        raises ``BMv2StartupError``. The caller remains responsible for stopping
        the child after failure; the context manager handles this automatically.

        Timeouts must be finite positive numbers, excluding bool. None uses
        the startup_timeout supplied at construction. Invalid values raise ValueError.
        """
        if not self._started or self._proc is None:
            raise BMv2StartupError(f"BMv2 {self._name!r} has not been started; call start() first")
        effective_timeout = self._startup_timeout if timeout is None else timeout
        _validate_timeout(effective_timeout, "timeout")
        deadline = time.monotonic() + effective_timeout
        start_time = time.monotonic()
        try:
            addresses = {
                ip_address(info[4][0])
                for info in socket.getaddrinfo(
                    self._grpc_bind_addr, self._grpc_port, type=socket.SOCK_STREAM
                )
            }
        except OSError as exc:
            raise BMv2StartupError(
                f"cannot resolve BMv2 gRPC address {self.grpc_address}: {exc}"
            ) from exc
        while time.monotonic() < deadline:
            if not self.is_running():
                rc = self.returncode()
                raise BMv2StartupError(
                    f"BMv2 {self._name!r} exited before becoming ready "
                    f"(returncode={rc}); see {self.log_file}"
                )
            try:
                grpc_inodes, thrift_inodes = self._listening_inodes(addresses)
                # Read descriptors after the listener snapshot so a newly opened
                # socket is not mistaken for a socket owned by another process.
                owned_inodes = self._socket_inodes()
            except (OSError, NetlinkError) as exc:
                if not self.is_running():
                    continue
                raise BMv2StartupError(
                    f"cannot inspect BMv2 {self._name!r} listeners (pid={self.pid}): {exc}"
                ) from exc
            for service, port, inodes in (
                ("gRPC", self._grpc_port, grpc_inodes),
                ("Thrift", self._thrift_port, thrift_inodes),
            ):
                if inodes - owned_inodes:
                    raise BMv2StartupError(
                        f"BMv2 {self._name!r} {service} port {port} is already in use "
                        f"by another process; see {self.log_file}"
                    )
            if grpc_inodes and thrift_inodes and self.is_running():
                elapsed = time.monotonic() - start_time
                logger.debug("BMv2 %r became ready after %.3fs", self._name, elapsed)
                return
            time.sleep(_READY_POLL_INTERVAL)
        raise BMv2StartupError(
            f"BMv2 {self._name!r} did not open its gRPC and Thrift ports within "
            f"{effective_timeout}s; see {self.log_file}"
        )

    def _listening_inodes(
        self, addresses: set[IPv4Address | IPv6Address]
    ) -> tuple[set[str], set[str]]:
        grpc_inodes: set[str] = set()
        thrift_inodes: set[str] = set()
        with DiagSocket() as diag:
            diag.bind()
            families = [socket.AF_INET]
            if socket.has_ipv6:
                families.append(socket.AF_INET6)
            for family in families:
                for listener in diag.get_sock_stats(family=family, states=1 << SS_LISTEN):
                    port = listener["idiag_sport"]
                    inode = f"socket:[{listener['idiag_inode']}]"
                    if port == self._grpc_port and _listener_matches(
                        listener["idiag_src"],
                        addresses,
                        ipv6_only=bool(listener.get_attr("INET_DIAG_SKV6ONLY")),
                    ):
                        grpc_inodes.add(inode)
                    # BMv2's Thrift server binds a wildcard address.
                    if port == self._thrift_port:
                        thrift_inodes.add(inode)
        return grpc_inodes, thrift_inodes

    def _socket_inodes(self) -> set[str]:
        inodes: set[str] = set()
        for fd in Path(f"/proc/{self.pid}/fd").iterdir():
            try:
                target = str(fd.readlink())
            except FileNotFoundError:
                continue  # The child may close a descriptor during the scan.
            if target.startswith("socket:["):
                inodes.add(target)
        return inodes

    def stop(self, *, timeout: float = 5.0) -> None:
        """Send SIGTERM, escalate to SIGKILL on timeout. Idempotent.

        The timeout must be a finite positive number, excluding bool. Invalid
        values raise ValueError before checking process state or sending signals.
        """
        _validate_timeout(timeout, "timeout")
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            self._cleanup_log()
            logger.debug("BMv2 %r already exited (rc=%s)", self._name, self._proc.returncode)
            return
        logger.debug("BMv2 %r SIGTERM", self._name)
        self._proc.terminate()
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.debug(
                "BMv2 %r did not exit in %.1fs after SIGTERM; sending SIGKILL",
                self._name,
                timeout,
            )
            self._proc.kill()
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Truly unkillable; bail without raising. The caller will see
                # the process via /proc and can decide what to do.
                logger.debug("BMv2 %r did not exit even after SIGKILL", self._name)
                return
        logger.debug("BMv2 %r exited with rc=%s", self._name, self._proc.returncode)
        self._cleanup_log()

    def kill(self) -> None:
        """Send SIGKILL immediately. Idempotent."""
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            self._cleanup_log()
            return
        logger.debug("BMv2 %r SIGKILL (direct)", self._name)
        self._proc.kill()
        try:
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            return
        self._cleanup_log()

    def is_running(self) -> bool:
        """``True`` while the BMv2 child process is still alive."""
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def returncode(self) -> int | None:
        """Process exit code, or ``None`` if it has not exited yet."""
        if self._proc is None:
            return None
        return self._proc.poll()

    def _cleanup_log(self) -> None:
        handle = self._log_file_handle
        # Reset the boot timestamp alongside log cleanup so it becomes None
        # on every shutdown path (stop / kill / already-exited).
        self._boot_timestamp_us = None
        if handle is None:
            return
        try:
            close = getattr(handle, "close", None)
            if callable(close):
                close()
        finally:
            self._log_file_handle = None

    # Context manager ----------------------------------------------------

    def __enter__(self) -> BMv2Switch:
        self.start()
        try:
            self.wait_until_ready()
        except BaseException:
            try:
                self.stop()
            finally:
                pass
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def __repr__(self) -> str:
        state = "running" if self.is_running() else "stopped"
        return (
            f"BMv2Switch(name={self._name!r}, grpc={self.grpc_address}, "
            f"pid={self.pid}, state={state})"
        )
