"""Unit tests for `p4net.network.orchestrator.Network`. All primitives mocked."""

from __future__ import annotations

import signal
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import pytest
from pytest_mock import MockerFixture

from p4net.network import Network, NetworkAlreadyRunningError, NodeNotFoundError
from p4net.network.orchestrator import (
    _add_durations,
    _format_duration_ns,
    _parse_duration_ns,
)
from p4net.topo import Link, LinkEndpoint, Topology

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_simple_topology() -> Topology:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24", mac="aa:bb:cc:dd:ee:01")
    t.add_host("h2", ip="10.0.0.2/24")
    t.add_switch("s1", p4_src=Path("simple_routing.p4"))
    t.add_link("h1", "s1", port_b=1)
    t.add_link("h2", "s1", port_b=2)
    return t


def _make_multi_link_host_topology() -> Topology:
    """h1 → s1 (first link, IP from Host); h1 → s2 (later, link override only)."""
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24", mac="aa:bb:cc:dd:ee:01")
    t.add_switch("s1", p4_src=Path("p1.p4"))
    t.add_switch("s2", p4_src=Path("p2.p4"))
    t.add_link("h1", "s1", port_b=1)
    t.add_link("h1", "s2", port_b=1, ip_a="172.16.0.1/24")
    return t


@pytest.fixture
def patched(mocker: MockerFixture, tmp_path: Path) -> dict[str, Any]:
    """Patch every primitive Network calls; return a dict of MagicMocks."""

    # NetworkNamespace
    ns_factory = mocker.patch("p4net.network.orchestrator.NetworkNamespace")

    def make_ns(name: str) -> MagicMock:
        m = MagicMock(name=f"ns-{name}")
        m.name = name
        m.exists = True
        return m

    ns_factory.side_effect = make_ns

    # VethPair
    veth_factory = mocker.patch("p4net.network.orchestrator.VethPair")
    veth_instances: list[MagicMock] = []

    def make_veth(*args: Any, **kwargs: Any) -> MagicMock:
        m = MagicMock(name=f"veth-{args}")
        veth_instances.append(m)
        return m

    veth_factory.side_effect = make_veth

    # apply_netem
    apply_netem = mocker.patch("p4net.network.orchestrator.apply_netem")

    # IPv6 sysctl helpers (no-op so the orchestrator's per-iface IPv6 gate
    # doesn't try to run real sysctl through a mocked NetworkNamespace).
    enable_ipv6_mock = mocker.patch("p4net.network.orchestrator.enable_ipv6")
    disable_ipv6_mock = mocker.patch("p4net.network.orchestrator.disable_ipv6")

    # P4Compiler
    compiler_factory = mocker.patch("p4net.network.orchestrator.P4Compiler")
    compiler = MagicMock(name="P4Compiler")
    compiler.compile = MagicMock(
        side_effect=lambda src, **kw: MagicMock(
            bmv2_json=tmp_path / f"{src.stem}.json",
            p4info=tmp_path / f"{src.stem}.p4info.txtpb",
        )
    )
    compiler_factory.return_value = compiler

    # BMv2Switch
    bmv2_factory = mocker.patch("p4net.network.orchestrator.BMv2Switch")
    bmv2_instances: list[MagicMock] = []

    def make_bmv2(name: str, **kw: Any) -> MagicMock:
        m = MagicMock(name=f"bmv2-{name}")
        m.name = name
        m.grpc_address = f"127.0.0.1:{kw.get('grpc_port', 50051)}"
        bmv2_instances.append(m)
        return m

    bmv2_factory.side_effect = make_bmv2

    # P4RuntimeClient
    client_factory = mocker.patch("p4net.network.orchestrator.P4RuntimeClient")
    client_instances: list[MagicMock] = []

    def make_client(target: str, **kw: Any) -> MagicMock:
        m = MagicMock(name=f"client-{target}")
        m.target = target
        client_instances.append(m)
        return m

    client_factory.side_effect = make_client

    # Cleanup hooks
    install_handlers = mocker.patch("p4net.network.orchestrator.install_handlers")
    register = mocker.patch("p4net.network.orchestrator.register")
    unregister = mocker.patch("p4net.network.orchestrator.unregister")

    return {
        "ns_factory": ns_factory,
        "veth_factory": veth_factory,
        "veths": veth_instances,
        "apply_netem": apply_netem,
        "enable_ipv6": enable_ipv6_mock,
        "disable_ipv6": disable_ipv6_mock,
        "compiler": compiler,
        "bmv2_factory": bmv2_factory,
        "bmv2s": bmv2_instances,
        "client_factory": client_factory,
        "clients": client_instances,
        "install_handlers": install_handlers,
        "register": register,
        "unregister": unregister,
    }


# ---------------------------------------------------------------------------
# Successful start/stop
# ---------------------------------------------------------------------------


def test_start_invokes_primitives_in_documented_order(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    topo = _make_simple_topology()
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        # Compiler called once per switch.
        patched["compiler"].compile.assert_called_once()
        # Two namespaces (h1, h2).
        assert patched["ns_factory"].call_count == 2
        # Two veth pairs (one per link).
        assert patched["veth_factory"].call_count == 2
        # One BMv2 switch (s1).
        assert patched["bmv2_factory"].call_count == 1
        # One P4Runtime client (s1).
        assert patched["client_factory"].call_count == 1
        # Cleanup hooks installed and Network registered.
        patched["install_handlers"].assert_called_once()
        patched["register"].assert_called_once_with(net)
        # Pipeline pushed.
        client = patched["clients"][0]
        client.connect.assert_called_once()
        client.set_pipeline_config.assert_called_once()
        # is_running and lookups work.
        assert net.is_running
        assert net.host("h1").name == "h1"
        assert net.switch("s1").name == "s1"
    finally:
        net.stop()


def test_stop_invokes_destructors_and_unregisters(patched: dict[str, Any], tmp_path: Path) -> None:
    topo = _make_simple_topology()
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    net.stop()
    # disconnect on every client, stop on every BMv2, destroy every veth+ns.
    for c in patched["clients"]:
        c.disconnect.assert_called()
    for b in patched["bmv2s"]:
        b.stop.assert_called()
    for v in patched["veths"]:
        v.destroy.assert_called()
    patched["unregister"].assert_called_with(net)
    assert net.is_running is False


def test_stop_is_idempotent(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    net.stop()
    net.stop()
    net.stop()
    # Each veth destroy called exactly once.
    for v in patched["veths"]:
        assert v.destroy.call_count == 1


def test_start_already_running_raises(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        with pytest.raises(NetworkAlreadyRunningError):
            net.start()
    finally:
        net.stop()


def test_lookup_unknown_raises(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        with pytest.raises(NodeNotFoundError):
            net.host("nope")
        with pytest.raises(NodeNotFoundError):
            net.switch("nope")
    finally:
        net.stop()


def test_unsafe_skips_validate(patched: dict[str, Any], tmp_path: Path) -> None:
    topo = _make_simple_topology()
    topo.validate = MagicMock(side_effect=AssertionError("validate must NOT run"))  # type: ignore[method-assign]
    net = Network(topo, log_dir=tmp_path / "logs", unsafe=True)
    net.start()
    topo.validate.assert_not_called()
    net.stop()


def test_safe_invokes_validate(patched: dict[str, Any], tmp_path: Path) -> None:
    topo = _make_simple_topology()
    topo.validate = MagicMock()  # type: ignore[method-assign]
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    topo.validate.assert_called_once()
    net.stop()


def test_extra_compile_args_passed_through(patched: dict[str, Any], tmp_path: Path) -> None:
    topo = _make_simple_topology()
    net = Network(
        topo,
        log_dir=tmp_path / "logs",
        extra_compile_args=("--Wdisable=unused", "-DSOME_FLAG"),
    )
    net.start()
    try:
        kwargs = patched["compiler"].compile.call_args.kwargs
        assert kwargs.get("extra_args") == ("--Wdisable=unused", "-DSOME_FLAG")
    finally:
        net.stop()


def test_extra_compile_args_default_is_empty_tuple(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        kwargs = patched["compiler"].compile.call_args.kwargs
        assert kwargs.get("extra_args") == ()
    finally:
        net.stop()


# ---------------------------------------------------------------------------
# Rollback paths
# ---------------------------------------------------------------------------


def test_rollback_on_link_creation_failure(patched: dict[str, Any], tmp_path: Path) -> None:
    veth = MagicMock()
    veth.create.side_effect = RuntimeError("veth create failed")
    patched["veth_factory"].side_effect = lambda *a, **kw: veth
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    with pytest.raises(RuntimeError, match="veth create failed"):
        net.start()
    # The Network is left not-running.
    assert net.is_running is False
    patched["unregister"].assert_called()


def test_rollback_on_bmv2_startup_failure(patched: dict[str, Any], tmp_path: Path) -> None:
    # Make wait_until_ready raise on the first BMv2.
    def make_bmv2(name: str, **kw: Any) -> MagicMock:
        m = MagicMock()
        m.name = name
        m.grpc_address = "127.0.0.1:0"
        m.wait_until_ready.side_effect = RuntimeError("bmv2 not ready")
        patched["bmv2s"].append(m)
        return m

    patched["bmv2_factory"].side_effect = make_bmv2
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    with pytest.raises(RuntimeError, match="not ready"):
        net.start()
    # Cleanup ran: namespaces destroyed, veths destroyed.
    for v in patched["veths"]:
        v.destroy.assert_called()
    assert net.is_running is False


def test_rollback_on_p4runtime_connect_failure(patched: dict[str, Any], tmp_path: Path) -> None:
    def make_client(target: str, **kw: Any) -> MagicMock:
        m = MagicMock()
        m.connect.side_effect = RuntimeError("connect failed")
        patched["clients"].append(m)
        return m

    patched["client_factory"].side_effect = make_client
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    with pytest.raises(RuntimeError, match="connect failed"):
        net.start()
    # BMv2 was started; rollback should have stopped it.
    for b in patched["bmv2s"]:
        b.stop.assert_called()
    assert net.is_running is False


# ---------------------------------------------------------------------------
# Multi-link host: first link gets Host.ip/Host.mac
# ---------------------------------------------------------------------------


def test_multi_link_host_first_link_inherits_ip_and_mac(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    topo = _make_multi_link_host_topology()
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        # Host-side configuration goes through ns.exec(['ip', ...]). Inspect the
        # h1 namespace mock's exec calls.
        h1_ns = net.host("h1").namespace
        argvs: list[list[str]] = [list(c.args[0]) for c in h1_ns.exec.call_args_list]
        flat = " ".join(" ".join(a) for a in argvs)
        assert "10.0.0.1/24" in flat  # Host.ip on first link
        assert "172.16.0.1/24" in flat  # link override on second link
        mac_addr_calls = [
            argv for argv in argvs if "address" in argv and "aa:bb:cc:dd:ee:01" in argv
        ]
        assert len(mac_addr_calls) == 1
    finally:
        net.stop()


# ---------------------------------------------------------------------------
# Election IDs uniqueness across Network instances
# ---------------------------------------------------------------------------


def test_election_ids_distinct_across_back_to_back_networks(
    patched: dict[str, Any], tmp_path: Path, mocker: MockerFixture
) -> None:
    captured_ids: list[tuple[int, int]] = []

    def make_client(target: str, **kw: Any) -> MagicMock:
        m = MagicMock()
        captured_ids.append(kw.get("election_id", (0, 0)))
        return m

    patched["client_factory"].side_effect = make_client
    # Make time.time_ns advance.
    times = iter([1_000_000_000, 2_000_000_000])
    mocker.patch("p4net.network.orchestrator.time.time_ns", side_effect=lambda: next(times))
    net1 = Network(_make_simple_topology(), log_dir=tmp_path / "logs1")
    net1.start()
    net1.stop()
    net2 = Network(_make_simple_topology(), log_dir=tmp_path / "logs2")
    net2.start()
    net2.stop()
    assert captured_ids[0] != captured_ids[1]


# ---------------------------------------------------------------------------
# Cleanup safety net
# ---------------------------------------------------------------------------


def test_signal_handlers_installed_in_main_thread(mocker: MockerFixture) -> None:
    # Need to reset the _INSTALLED flag for this test.
    import p4net.network._cleanup as cleanup

    mocker.patch.object(cleanup, "_INSTALLED", False)
    mocker.patch.object(cleanup, "_PREV_HANDLERS", {})
    fake_signal = mocker.patch.object(cleanup.signal, "signal")
    mocker.patch.object(cleanup.threading, "current_thread", return_value=threading.main_thread())
    mocker.patch.object(cleanup.atexit, "register")
    cleanup.install_handlers()
    sigs_seen = [c.args[0] for c in fake_signal.call_args_list]
    assert signal.SIGINT in sigs_seen
    assert signal.SIGTERM in sigs_seen


def test_signal_handlers_skipped_in_non_main_thread(mocker: MockerFixture) -> None:
    import p4net.network._cleanup as cleanup

    mocker.patch.object(cleanup, "_INSTALLED", False)
    mocker.patch.object(cleanup, "_PREV_HANDLERS", {})
    fake_signal = mocker.patch.object(cleanup.signal, "signal")
    fake_thread = MagicMock()
    fake_thread.__ne__ = lambda self, other: True  # type: ignore[assignment]
    mocker.patch.object(cleanup.threading, "current_thread", return_value=fake_thread)
    mocker.patch.object(cleanup.atexit, "register")
    cleanup.install_handlers()
    fake_signal.assert_not_called()


def test_install_handlers_idempotent(mocker: MockerFixture) -> None:
    import p4net.network._cleanup as cleanup

    mocker.patch.object(cleanup, "_INSTALLED", False)
    mocker.patch.object(cleanup, "_PREV_HANDLERS", {})
    fake_atexit = mocker.patch.object(cleanup.atexit, "register")
    mocker.patch.object(cleanup.signal, "signal")
    mocker.patch.object(cleanup.threading, "current_thread", return_value=threading.main_thread())
    cleanup.install_handlers()
    cleanup.install_handlers()
    cleanup.install_handlers()
    # atexit.register only called once.
    fake_atexit.assert_called_once()


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_context_manager_stops_on_exception(patched: dict[str, Any], tmp_path: Path) -> None:
    with (
        pytest.raises(ValueError, match="boom"),
        Network(_make_simple_topology(), log_dir=tmp_path / "logs"),
    ):
        raise ValueError("boom")
    # The veth/bmv2/client/ns cleanup should still have run.
    for v in patched["veths"]:
        v.destroy.assert_called()


# ---------------------------------------------------------------------------
# log_dir property defaults
# ---------------------------------------------------------------------------


def test_log_dir_defaults_to_tempdir_when_none(
    patched: dict[str, Any], tmp_path: Path, mocker: MockerFixture
) -> None:
    mocker.patch(
        "p4net.network.orchestrator.tempfile.mkdtemp",
        return_value=str(tmp_path / "auto-logs"),
    )
    net = Network(_make_simple_topology())
    net.start()
    try:
        assert net.log_dir == tmp_path / "auto-logs"
    finally:
        net.stop()


def test_log_dir_raises_before_start(patched: dict[str, Any]) -> None:
    net = Network(_make_simple_topology())
    with pytest.raises(RuntimeError, match="call start"):
        _ = net.log_dir


# ---------------------------------------------------------------------------
# Topology iface_name resolution feeds BMv2 port_to_iface
# ---------------------------------------------------------------------------


def test_bmv2_port_to_iface_built_from_topology(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        bmv2_call = patched["bmv2_factory"].call_args
        port_to_iface = bmv2_call.kwargs["port_to_iface"]
        # h1<->s1 used port 1; h2<->s1 used port 2; ifaces follow `<sw>-eth<port>`.
        assert port_to_iface == {1: "s1-eth1", 2: "s1-eth2"}
    finally:
        net.stop()


# ---------------------------------------------------------------------------
# Pickup a few assertions inline
# ---------------------------------------------------------------------------


def test_install_handlers_called_before_namespaces(patched: dict[str, Any], tmp_path: Path) -> None:
    """Cleanup hooks must be installed BEFORE the first NetworkNamespace is
    created so a Ctrl-C between the first namespace and the rest still tears
    things down.
    """
    order: list[str] = []
    patched["install_handlers"].side_effect = lambda: order.append("install")
    real_make_ns = patched["ns_factory"].side_effect

    def trace_ns(name: str) -> Any:
        order.append("ns")
        return real_make_ns(name)

    patched["ns_factory"].side_effect = trace_ns
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        assert order.index("install") < order.index("ns")
    finally:
        net.stop()


def test_stop_before_start_is_noop(patched: dict[str, Any]) -> None:
    net = Network(_make_simple_topology())
    net.stop()  # Must not raise.
    assert net.is_running is False


def test_calls_at_least_for_call(patched: dict[str, Any]) -> None:
    """Sanity: `call` is importable from unittest.mock so the test file imports cleanly."""
    _ = call  # touch to silence unused-import warnings if ruff hides them


# ---------------------------------------------------------------------------
# ping / pingall
# ---------------------------------------------------------------------------


def test_ping_resolves_host_names(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        # Replace each running host's namespace.exec with a controllable mock.
        h1 = net.host("h1")
        h2 = net.host("h2")
        h1.namespace.exec = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(returncode=0)
        )
        # Ping by name → uses h2's primary_ip.
        ok = net.ping("h1", "h2", count=2, timeout=1.0)
        assert ok is True
        argv = h1.namespace.exec.call_args.args[0]
        assert argv[:6] == ["ping", "-4", "-c", "2", "-W", "1"]
        assert argv[-1] == h2.primary_ip
    finally:
        net.stop()


def test_ping_with_running_host_objects(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        h1 = net.host("h1")
        h2 = net.host("h2")
        h1.namespace.exec = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(returncode=0)
        )
        assert net.ping(h1, h2) is True
    finally:
        net.stop()


def test_ping_with_unknown_dst_passes_string_through(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        h1 = net.host("h1")
        h1.namespace.exec = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(returncode=1)
        )
        # An IP literal that's not a known host name is passed through.
        result = net.ping("h1", "203.0.113.5")
        assert result is False
        argv = h1.namespace.exec.call_args.args[0]
        assert argv[-1] == "203.0.113.5"
    finally:
        net.stop()


def test_pingall_returns_pair_results(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        for name in ("h1", "h2"):
            h = net.host(name)
            h.namespace.exec = MagicMock(  # type: ignore[method-assign]
                return_value=MagicMock(returncode=0)
            )
        result = net.pingall()
        assert set(result.keys()) == {("h1", "h2"), ("h2", "h1")}
        assert all(result.values())
    finally:
        net.stop()


def test_pingall_skips_hosts_without_primary_ip(patched: dict[str, Any], tmp_path: Path) -> None:
    topo = Topology()
    topo.add_host("h1", ip="10.0.0.1/24")
    topo.add_host("h2")  # no IP
    topo.add_switch("s1", p4_src=Path("p.p4"))
    topo.add_link("h1", "s1")
    topo.add_link("h2", "s1")
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        h1 = net.host("h1")
        h1.namespace.exec = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(returncode=0)
        )
        result = net.pingall()
        # h2 has no primary_ip, so no pings involving h2 should be issued.
        assert result == {}
    finally:
        net.stop()


# ---------------------------------------------------------------------------
# IPv6 + asymmetric netem (phase 12)
# ---------------------------------------------------------------------------


def _make_v4_only_topology() -> Topology:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24")
    t.add_switch("s1", p4_src=Path("p.p4"))
    t.add_link("h1", "s1", port_b=1)
    return t


def _make_v6_only_topology() -> Topology:
    t = Topology()
    t.add_host("h1", ip6="fd00::1/64")
    t.add_switch("s1", p4_src=Path("p.p4"))
    t.add_link("h1", "s1", port_b=1)
    return t


def _make_dual_stack_topology() -> Topology:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24", ip6="fd00::1/64")
    t.add_switch("s1", p4_src=Path("p.p4"))
    t.add_link("h1", "s1", port_b=1)
    return t


def test_ipv4_only_host_disables_ipv6_on_host_iface(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    net = Network(_make_v4_only_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        host_calls = [c for c in patched["disable_ipv6"].call_args_list if c.args[0] is not None]
        # The h1-side iface gets disable_ipv6 invoked.
        assert any(c.args[1] == "h1-eth0" for c in host_calls)
        # enable_ipv6 NOT called on the host iface.
        for c in patched["enable_ipv6"].call_args_list:
            assert c.args[1] != "h1-eth0"
    finally:
        net.stop()


def test_ipv6_only_host_enables_ipv6_on_host_iface(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_v6_only_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        # enable_ipv6 called for h1-eth0 inside h1's namespace.
        assert any(
            c.args[1] == "h1-eth0" and c.args[0] is not None
            for c in patched["enable_ipv6"].call_args_list
        )
        # IP6 was assigned via the host-side `ip -6 addr add` path.
        h1_ns = net._namespaces["h1"]  # type: ignore[attr-defined]
        argvs = [c.args[0] for c in h1_ns.exec.call_args_list]
        assert any(argv[:3] == ["ip", "-6", "addr"] and "fd00::1/64" in argv for argv in argvs)
    finally:
        net.stop()


def test_dual_stack_host_runs_both_paths(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_dual_stack_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        # enable_ipv6 was called for the host iface.
        assert any(
            c.args[1] == "h1-eth0" and c.args[0] is not None
            for c in patched["enable_ipv6"].call_args_list
        )
        h1_ns = net._namespaces["h1"]  # type: ignore[attr-defined]
        argvs = [c.args[0] for c in h1_ns.exec.call_args_list]
        # IPv4 and IPv6 both assigned.
        assert any(argv[:3] == ["ip", "addr", "add"] and "10.0.0.1/24" in argv for argv in argvs)
        assert any(argv[:3] == ["ip", "-6", "addr"] and "fd00::1/64" in argv for argv in argvs)
    finally:
        net.stop()


def test_switch_iface_disables_ipv6(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_v4_only_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        # disable_ipv6 called with ns=None for the switch-side iface (s1-eth1).
        root_calls = [c for c in patched["disable_ipv6"].call_args_list if c.args[0] is None]
        assert any(c.args[1] == "s1-eth1" for c in root_calls)
    finally:
        net.stop()


def test_default_route6_is_added(patched: dict[str, Any], tmp_path: Path) -> None:
    t = Topology()
    t.add_host("h1", ip6="fd00::2/64", default_route6="fd00::1")
    t.add_switch("s1", p4_src=Path("p.p4"))
    t.add_link("h1", "s1", port_b=1)
    net = Network(t, log_dir=tmp_path / "logs")
    net.start()
    try:
        h1_ns = net._namespaces["h1"]  # type: ignore[attr-defined]
        argvs = [c.args[0] for c in h1_ns.exec.call_args_list]
        assert ["ip", "-6", "route", "add", "default", "via", "fd00::1"] in argvs
    finally:
        net.stop()


def test_asymmetric_loss_only_a_to_b(patched: dict[str, Any], tmp_path: Path) -> None:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24")
    t.add_switch("s1", p4_src=Path("p.p4"))
    t.add_link("h1", "s1", port_b=1, loss_pct_a_to_b=100.0)
    net = Network(t, log_dir=tmp_path / "logs")
    net.start()
    try:
        # apply_netem called once: on the a-side (h1-eth0) with loss_pct=100.0.
        calls = patched["apply_netem"].call_args_list
        assert len(calls) == 1
        # ns positional arg is the host's namespace; iface is h1-eth0.
        assert calls[0].args[1] == "h1-eth0"
        assert calls[0].kwargs["loss_pct"] == 100.0
    finally:
        net.stop()


def test_symmetric_bandwidth_with_asymmetric_delay_a_to_b(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24")
    t.add_switch("s1", p4_src=Path("p.p4"))
    t.add_link("h1", "s1", port_b=1, bandwidth="10mbit", delay_a_to_b="5ms")
    net = Network(t, log_dir=tmp_path / "logs")
    net.start()
    try:
        calls = patched["apply_netem"].call_args_list
        # Two calls: a-side (h1-eth0) with rate+delay, b-side (s1-eth1) with rate only.
        assert len(calls) == 2
        ifaces = {c.args[1]: c.kwargs for c in calls}
        assert ifaces["h1-eth0"]["rate"] == "10mbit"
        assert ifaces["h1-eth0"]["delay"] == "5ms"
        assert ifaces["s1-eth1"]["rate"] == "10mbit"
        assert ifaces["s1-eth1"]["delay"] is None
    finally:
        net.stop()


# ---------------------------------------------------------------------------
# xterm + pingall6 (phase 13)
# ---------------------------------------------------------------------------


def test_xterm_runs_xterm_in_host_namespace(
    patched: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        h1 = net.host("h1")
        fake_proc = MagicMock(name="NSProcess")
        fake_proc.pid = 999
        fake_proc.poll = MagicMock(return_value=None)
        h1.namespace.popen = MagicMock(return_value=fake_proc)  # type: ignore[method-assign]
        proc = net.xterm("h1")
        assert proc is fake_proc
        argv = h1.namespace.popen.call_args.args[0]
        assert argv[:5] == ["xterm", "-T", "p4net: h1", "-e", "bash"]
    finally:
        net.stop()


def test_xterm_honours_title_and_shell(
    patched: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        h1 = net.host("h1")
        fake_proc = MagicMock(name="NSProcess")
        fake_proc.pid = 1
        fake_proc.poll = MagicMock(return_value=None)
        h1.namespace.popen = MagicMock(return_value=fake_proc)  # type: ignore[method-assign]
        net.xterm("h1", title="custom", shell="zsh")
        argv = h1.namespace.popen.call_args.args[0]
        assert argv[:5] == ["xterm", "-T", "custom", "-e", "zsh"]
    finally:
        net.stop()


def test_xterm_raises_when_display_unset(
    patched: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    from p4net.network import NetworkError

    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        with pytest.raises(NetworkError, match="DISPLAY is unset"):
            net.xterm("h1")
    finally:
        net.stop()


def test_xterm_terminated_on_stop(
    patched: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    h1 = net.host("h1")
    fake_proc = MagicMock(name="NSProcess")
    fake_proc.pid = 1
    fake_proc.poll = MagicMock(return_value=None)
    fake_proc.wait = MagicMock(return_value=0)
    h1.namespace.popen = MagicMock(return_value=fake_proc)  # type: ignore[method-assign]
    net.xterm("h1")
    net.stop()
    fake_proc.terminate.assert_called()


def test_already_exited_spawn_skips_terminate_and_wait(
    patched: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If an xterm has already exited (user closed the window), Network.stop()
    must not call terminate() or wait() on it — only clear it from the list."""
    monkeypatch.setenv("DISPLAY", ":0")
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    h1 = net.host("h1")
    fake_proc = MagicMock(name="NSProcess")
    fake_proc.pid = 1
    # poll() returns exit code 0 — process already finished.
    fake_proc.poll = MagicMock(return_value=0)
    h1.namespace.popen = MagicMock(return_value=fake_proc)  # type: ignore[method-assign]
    net.xterm("h1")
    net.stop()
    fake_proc.terminate.assert_not_called()
    fake_proc.wait.assert_not_called()
    fake_proc.kill.assert_not_called()


def test_pingall6_skips_v4_only_hosts(patched: dict[str, Any], tmp_path: Path) -> None:
    topo = Topology()
    topo.add_host("h1", ip="10.0.0.1/24", ip6="fd00::1/64")
    topo.add_host("h2", ip="10.0.0.2/24", ip6="fd00::2/64")
    topo.add_host("h3", ip="10.0.0.3/24")  # no ip6
    topo.add_switch("s1", p4_src=Path("p.p4"))
    topo.add_link("h1", "s1")
    topo.add_link("h2", "s1")
    topo.add_link("h3", "s1")
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        # Reset every host's namespace.exec so we only count pingall6's calls.
        for h_name in ("h1", "h2", "h3"):
            h = net.host(h_name)
            h.namespace.exec = MagicMock(  # type: ignore[method-assign]
                return_value=MagicMock(returncode=0)
            )
        result = net.pingall6()
        # Two ordered pairs over {h1, h2}: (h1,h2) and (h2,h1). h3 absent.
        assert set(result.keys()) == {("h1", "h2"), ("h2", "h1")}
        # Verify a real call: h1 used force_ipv6=True via the `-6` argv flag.
        argv = net.host("h1").namespace.exec.call_args.args[0]
        assert "-6" in argv
        # h3 received no pingall6-driven exec.
        net.host("h3").namespace.exec.assert_not_called()
    finally:
        net.stop()


def test_pingall6_empty_when_no_v6_hosts(patched: dict[str, Any], tmp_path: Path) -> None:
    topo = Topology()
    topo.add_host("h1", ip="10.0.0.1/24")
    topo.add_switch("s1", p4_src=Path("p.p4"))
    topo.add_link("h1", "s1")
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        assert net.pingall6() == {}
    finally:
        net.stop()


# ---------------------------------------------------------------------------
# _direction_params with extras
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected_ns"),
    [
        ("100ms", 100_000_000),
        ("1s", 1_000_000_000),
        ("250us", 250_000),
        ("500ns", 500),
        ("0ms", 0),
    ],
)
def test_parse_duration_ns(value: str, expected_ns: int) -> None:
    assert _parse_duration_ns(value) == expected_ns


@pytest.mark.parametrize(
    ("ns", "expected"),
    [
        (100_000_000, "100ms"),
        (1_000_000_000, "1s"),
        (1_500_000_000, "1500ms"),
        (250_000, "250us"),
        (500, "500ns"),
    ],
)
def test_format_duration_ns(ns: int, expected: str) -> None:
    assert _format_duration_ns(ns) == expected


def test_add_durations_sums_canonical() -> None:
    assert _add_durations("100ms", "50ms") == "150ms"
    assert _add_durations("1s", "500ms") == "1500ms"
    assert _add_durations("100us", "1ms") == "1100us"


def test_direction_params_extra_adds_to_symmetric_delay() -> None:
    link = Link(
        a=LinkEndpoint(node="h1", iface_name="h1-eth0"),
        b=LinkEndpoint(node="s1", iface_name="s1-eth0"),
        delay="100ms",
        delay_a_to_b_extra="50ms",
    )
    _rate, delay, jitter, loss = Network._direction_params(link, "a_to_b")
    assert delay == "150ms"
    assert jitter is None
    assert loss is None
    _rate, delay, _jitter, _loss = Network._direction_params(link, "b_to_a")
    assert delay == "100ms"


def test_direction_params_no_extra_returns_base() -> None:
    link = Link(
        a=LinkEndpoint(node="h1", iface_name="h1-eth0"),
        b=LinkEndpoint(node="s1", iface_name="s1-eth0"),
        delay="100ms",
    )
    _, delay_ab, _, _ = Network._direction_params(link, "a_to_b")
    _, delay_ba, _, _ = Network._direction_params(link, "b_to_a")
    assert delay_ab == "100ms"
    assert delay_ba == "100ms"


def test_direction_params_loss_extra_sums() -> None:
    link = Link(
        a=LinkEndpoint(node="h1", iface_name="h1-eth0"),
        b=LinkEndpoint(node="s1", iface_name="s1-eth0"),
        loss_pct=1.0,
        loss_pct_a_to_b_extra=4.0,
    )
    _, _, _, loss_ab = Network._direction_params(link, "a_to_b")
    _, _, _, loss_ba = Network._direction_params(link, "b_to_a")
    assert loss_ab == 5.0
    assert loss_ba == 1.0


def test_link_round_trip_preserves_extras() -> None:
    topo = Topology()
    topo.add_host("h1", ip="10.0.0.1/24")
    topo.add_switch("s1", p4_src=Path("p.p4"))
    topo.add_link(
        "h1",
        "s1",
        delay="100ms",
        delay_a_to_b_extra="50ms",
        loss_pct=1.0,
        loss_pct_b_to_a_extra=2.0,
    )
    round_tripped = Topology.from_dict(topo.to_dict())
    link = next(iter(round_tripped.links))
    assert link.delay_a_to_b_extra == "50ms"
    assert link.delay_b_to_a_extra is None
    assert link.loss_pct_b_to_a_extra == 2.0
    assert link.loss_pct_a_to_b_extra is None


# ---------------------------------------------------------------------------
# RunningSwitch.boot_timestamp_us
# ---------------------------------------------------------------------------


def test_running_switch_boot_timestamp_proxies_bmv2() -> None:
    from p4net.network.nodes import RunningSwitch
    from p4net.topo import P4Switch

    sw_desc = P4Switch(name="s1", p4_src=Path("p.p4"))
    bmv2 = MagicMock()
    bmv2.boot_timestamp_us = 1_736_700_000_000_000
    rs = RunningSwitch(sw_desc, bmv2, MagicMock(), MagicMock())
    assert rs.boot_timestamp_us == 1_736_700_000_000_000


def test_running_switch_boot_timestamp_raises_when_none() -> None:
    from p4net.network.exceptions import NetworkNotRunningError
    from p4net.network.nodes import RunningSwitch
    from p4net.topo import P4Switch

    sw_desc = P4Switch(name="s1", p4_src=Path("p.p4"))
    bmv2 = MagicMock()
    bmv2.boot_timestamp_us = None
    rs = RunningSwitch(sw_desc, bmv2, MagicMock(), MagicMock())
    with pytest.raises(NetworkNotRunningError, match="no boot timestamp"):
        _ = rs.boot_timestamp_us


# ---------------------------------------------------------------------------
# Network.boot_timestamps
# ---------------------------------------------------------------------------


def test_network_boot_timestamps_returns_dict_when_running(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    # Stamp each mock BMv2 with a distinct integer so the property has real
    # ints to surface (the default MagicMock attribute is itself a MagicMock).
    topo = _make_simple_topology()
    net = Network(topo, log_dir=tmp_path / "logs")
    net.start()
    try:
        for i, bmv2 in enumerate(patched["bmv2s"], start=1):
            bmv2.boot_timestamp_us = 1_736_700_000_000_000 + i
        got = net.boot_timestamps
        assert set(got.keys()) == set(topo.switches)
        assert all(isinstance(v, int) for v in got.values())
        # Values match what individual RunningSwitch.boot_timestamp_us returns.
        for name, value in got.items():
            assert net.switch(name).boot_timestamp_us == value
    finally:
        net.stop()


def test_network_boot_timestamps_returns_fresh_dict(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        for i, bmv2 in enumerate(patched["bmv2s"], start=1):
            bmv2.boot_timestamp_us = 1000 + i
        a = net.boot_timestamps
        a["s1"] = 999_999  # mutate
        b = net.boot_timestamps
        assert b["s1"] != 999_999  # not the same dict
    finally:
        net.stop()


def test_network_boot_timestamps_raises_when_not_running(tmp_path: Path) -> None:
    from p4net.network.exceptions import NetworkNotRunningError

    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    with pytest.raises(NetworkNotRunningError, match="not running"):
        _ = net.boot_timestamps


# ---------------------------------------------------------------------------
# RunningSwitch.async_client
# ---------------------------------------------------------------------------


def test_running_switch_async_client_lazy_cached() -> None:
    from p4net.control import AsyncP4RuntimeClient
    from p4net.network.nodes import RunningSwitch
    from p4net.topo import P4Switch

    bmv2 = MagicMock()
    bmv2.grpc_port = 50051
    bmv2.thrift_port = 9090
    bmv2.device_id = 0
    sync_client = MagicMock()
    sync_client._index = MagicMock()
    rs = RunningSwitch(
        P4Switch(name="s1", p4_src=Path("p.p4")),
        bmv2,
        sync_client,
        MagicMock(),
    )
    ac = rs.async_client
    assert isinstance(ac, AsyncP4RuntimeClient)
    # Cached.
    assert rs.async_client is ac
    # Plumbed through.
    assert ac.grpc_address == ("127.0.0.1", 50051)
    assert ac.device_id == 0
    assert ac._thrift_address == ("127.0.0.1", 9090)
    assert ac._info_index is sync_client._index


def test_running_switch_async_client_reset_after_stop(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    """Network.stop() drops the cached async client on each RunningSwitch."""
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    sw = net.switch("s1")
    first = sw.async_client
    assert first is sw.async_client  # cached
    net.stop()
    # The orchestrator clears the running-switches dict on stop AND zeroes
    # the per-RunningSwitch async cache via _reset_async_client.
    assert sw._async_client is None
