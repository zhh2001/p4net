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
from p4net.topo import Topology

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
        assert argv[:5] == ["ping", "-c", "2", "-W", "1"]
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
