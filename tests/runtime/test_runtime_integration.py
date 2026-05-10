"""End-to-end integration tests for the runtime primitives.

These exercise real Linux network namespaces, real veth devices, and real
tc/netem state. They require root and a Linux host. The session-level gating
lives in `tests/conftest.py` (the `--run-integration` flag plus the
`integration` marker).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator

import pytest

from p4net.runtime import (
    NamespaceError,
    NetworkNamespace,
    VethPair,
    apply_netem,
    clear_qdisc,
    disable_ipv6,
    enable_ipv6,
)

pytestmark = pytest.mark.integration


def _rand(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


@contextlib.contextmanager
def _safe_destroy_ns(ns: NetworkNamespace) -> Iterator[NetworkNamespace]:
    try:
        yield ns
    finally:
        try:
            if ns.exists:
                ns.destroy()
        except (NamespaceError, OSError):
            pass


def test_namespace_exec_lo_only() -> None:
    """A fresh namespace should expose only the loopback interface."""
    ns = NetworkNamespace(_rand("ns"))
    ns.create()
    with _safe_destroy_ns(ns):
        result = ns.exec(["ip", "-o", "link", "show"], capture_output=True)
        out = result.stdout.decode()
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected only lo, got {len(lines)} lines:\n{out}"
        assert " lo:" in lines[0] or lines[0].startswith("1: lo"), out


def test_veth_ping_then_loss_then_clear() -> None:
    """Ping, apply 100% loss, ping fails, clear qdisc, ping succeeds again."""
    ns_a = NetworkNamespace(_rand("nsA_"))
    ns_b = NetworkNamespace(_rand("nsB_"))
    veth = VethPair(_rand("vA_"), _rand("vB_"))
    cleanup_veth = False
    try:
        ns_a.create()
        ns_b.create()
        veth.create()
        cleanup_veth = True
        veth.move_to_namespace("a", ns_a)
        veth.move_to_namespace("b", ns_b)
        veth.set_address("a", "10.250.0.1/24")
        veth.set_address("b", "10.250.0.2/24")
        veth.set_up("a")
        veth.set_up("b")
        ns_a.exec(["ip", "link", "set", "lo", "up"])
        ns_b.exec(["ip", "link", "set", "lo", "up"])

        first = ns_a.exec(
            ["ping", "-c", "1", "-W", "2", "10.250.0.2"],
            capture_output=True,
            check=False,
        )
        assert first.returncode == 0, (
            f"initial ping failed: rc={first.returncode} "
            f"stderr={first.stderr.decode(errors='replace')!r}"
        )

        apply_netem(ns_a, veth.name_a, loss_pct=100.0)
        lossy = ns_a.exec(
            ["ping", "-c", "1", "-W", "2", "10.250.0.2"],
            capture_output=True,
            check=False,
        )
        assert lossy.returncode != 0, "ping should fail under 100% loss"

        clear_qdisc(ns_a, veth.name_a)
        recovered = ns_a.exec(
            ["ping", "-c", "1", "-W", "2", "10.250.0.2"],
            capture_output=True,
            check=False,
        )
        assert recovered.returncode == 0, (
            f"ping after clear failed: rc={recovered.returncode} "
            f"stderr={recovered.stderr.decode(errors='replace')!r}"
        )
    finally:
        if cleanup_veth:
            with contextlib.suppress(Exception):
                veth.destroy()
        for ns in (ns_a, ns_b):
            with contextlib.suppress(Exception):
                if ns.exists:
                    ns.destroy()


def test_ipv6_sysctl_and_address_round_trip() -> None:
    """disable_ipv6 / enable_ipv6 / set_address6 against a real namespace."""
    ns = NetworkNamespace(_rand("ns"))
    veth = VethPair(_rand("vE_"), _rand("vF_"))
    try:
        ns.create()
        veth.create()
        veth.move_to_namespace("a", ns)
        veth.set_up("a")
        iface = veth.name_a

        disable_ipv6(ns, iface)
        out = ns.exec(
            ["sysctl", "-n", f"net.ipv6.conf.{iface}.disable_ipv6"],
            capture_output=True,
        )
        assert out.stdout.decode().strip() == "1"

        enable_ipv6(ns, iface)
        out = ns.exec(
            ["sysctl", "-n", f"net.ipv6.conf.{iface}.disable_ipv6"],
            capture_output=True,
        )
        assert out.stdout.decode().strip() == "0"
        out = ns.exec(
            ["sysctl", "-n", f"net.ipv6.conf.{iface}.accept_ra"],
            capture_output=True,
        )
        assert out.stdout.decode().strip() == "0"

        veth.set_address6("a", "fd00::1/64")
        out = ns.exec(["ip", "-6", "addr", "show", "dev", iface], capture_output=True)
        assert b"fd00::1/64" in out.stdout
    finally:
        with contextlib.suppress(Exception):
            veth.destroy()
        with contextlib.suppress(Exception):
            if ns.exists:
                ns.destroy()


def test_clear_qdisc_idempotent_when_nothing_set() -> None:
    """clear_qdisc on an interface with no netem qdisc must not raise."""
    ns = NetworkNamespace(_rand("ns"))
    veth = VethPair(_rand("vC_"), _rand("vD_"))
    try:
        ns.create()
        veth.create()
        veth.move_to_namespace("a", ns)
        veth.set_up("a")
        # Should be a no-op (the kernel default is not netem).
        clear_qdisc(ns, veth.name_a)
        clear_qdisc(ns, veth.name_a)
    finally:
        with contextlib.suppress(Exception):
            veth.destroy()
        with contextlib.suppress(Exception):
            if ns.exists:
                ns.destroy()
