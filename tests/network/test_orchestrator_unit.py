"""Unit tests for `p4net.network.orchestrator.Network`. All primitives mocked."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

import pytest
from pytest_mock import MockerFixture

from p4net.network import Network, NetworkAlreadyRunningError, NetworkError, NodeNotFoundError
from p4net.network.orchestrator import (
    _add_durations,
    _format_duration_ns,
    _parse_duration_ns,
)
from p4net.topo import Link, LinkEndpoint, Topology, TopologyError

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


def test_cpu_port_conflict_rejected_before_runtime_resources(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    data = _make_simple_topology().to_dict()
    data["switches"]["s1"]["cpu_port"] = 1
    network = Network(Topology.from_dict(data), log_dir=tmp_path / "logs")
    try:
        with pytest.raises(TopologyError, match="CPU port"):
            network.start()
        for name in ("ns_factory", "veth_factory", "bmv2_factory", "client_factory"):
            patched[name].assert_not_called()
        patched["compiler"].compile.assert_not_called()
    finally:
        network.stop()


@pytest.mark.parametrize("port", [511, 512])
def test_imported_switch_port_rejected_before_runtime_resources(
    patched: dict[str, Any], tmp_path: Path, port: int
) -> None:
    data = _make_simple_topology().to_dict()
    data["links"][0]["b"]["port"] = port
    network = Network(Topology.from_dict(data), log_dir=tmp_path / "logs")
    try:
        with pytest.raises(TopologyError, match="port"):
            network.start()
        for name in ("ns_factory", "veth_factory", "bmv2_factory", "client_factory"):
            patched[name].assert_not_called()
        patched["compiler"].compile.assert_not_called()
    finally:
        network.stop()


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("missing", ["omitted", "null"])
def test_imported_unassigned_port_rejected_before_runtime_resources(
    patched: dict[str, Any], tmp_path: Path, side: str, missing: str
) -> None:
    data = _make_simple_topology().to_dict()
    if missing == "omitted":
        data["links"][0][side].pop("port")
    else:
        data["links"][0][side]["port"] = None
    topo = Topology.from_dict(data)
    before = topo.to_dict()
    network = Network(topo, log_dir=tmp_path / "logs")
    try:
        with pytest.raises(TopologyError, match="has no assigned port"):
            network.start()
        for name in ("ns_factory", "veth_factory", "bmv2_factory", "client_factory"):
            patched[name].assert_not_called()
        patched["compiler"].compile.assert_not_called()
        assert topo.to_dict() == before
    finally:
        network.stop()


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("missing", ["omitted", "null", "empty"])
def test_imported_unassigned_interface_name_rejected_before_runtime_resources(
    patched: dict[str, Any], tmp_path: Path, side: str, missing: str
) -> None:
    data = _make_simple_topology().to_dict()
    if missing == "omitted":
        data["links"][0][side].pop("iface_name")
    else:
        data["links"][0][side]["iface_name"] = None if missing == "null" else ""
    topo = Topology.from_dict(data)
    before = topo.to_dict()
    logs = tmp_path / "logs"
    network = Network(topo, log_dir=logs)
    try:
        with pytest.raises(TopologyError, match="has no assigned interface name"):
            network.start()
        for name in ("ns_factory", "veth_factory", "bmv2_factory", "client_factory"):
            patched[name].assert_not_called()
        patched["compiler"].compile.assert_not_called()
        assert not logs.exists()
        assert topo.to_dict() == before
    finally:
        network.stop()


@pytest.fixture
def patched(mocker: MockerFixture, tmp_path: Path) -> dict[str, Any]:
    """Patch every primitive Network calls; return a dict of MagicMocks."""

    # NetworkNamespace
    ns_factory = mocker.patch("p4net.network.orchestrator.NetworkNamespace")

    def make_ns(name: str) -> MagicMock:
        m = MagicMock(name=f"ns-{name}")
        m.name = name
        m.exists = True
        addresses: dict[str, str] = {}

        def execute(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if argv[:4] == ["ip", "-6", "addr", "add"]:
                addresses[argv[-1]] = str(ipaddress.IPv6Interface(argv[4]).ip)
            data = [
                {"ifname": iface, "addr_info": [{"local": address}]}
                for iface, address in addresses.items()
            ]
            output = (
                json.dumps(data).encode() if argv == ["ip", "-j", "-6", "addr", "show"] else b""
            )
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr=b"")

        m.exec.side_effect = execute
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


@pytest.mark.parametrize("capture_enabled", [False, True])
@pytest.mark.parametrize("roundtrip", [False, True])
def test_per_switch_capture_settings(
    patched: dict[str, Any], tmp_path: Path, capture_enabled: bool, roundtrip: bool
) -> None:
    topo = _make_simple_topology()
    topo.add_switch("s2", p4_src=Path("simple_routing.p4"), pcap_enabled=False)
    topo.add_link("s1", "s2", port_a=3, port_b=1)
    if roundtrip:
        topo = Topology.from_dict(topo.to_dict())
    pcap_dir = tmp_path / "pcaps" if capture_enabled else None

    with Network(topo, log_dir=tmp_path / "logs", pcap_dir=pcap_dir):
        capture_dirs = {
            args.args[0]: args.kwargs["pcap_dir"] for args in patched["bmv2_factory"].call_args_list
        }
        assert capture_dirs == {"s1": pcap_dir, "s2": None}


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


@pytest.mark.parametrize("same_switch", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_service_port_collision_fails_before_creating_resources(
    patched: dict[str, Any], tmp_path: Path, same_switch: bool, asynchronous: bool
) -> None:
    topo = Topology()
    topo.add_host("h1")
    topo.add_switch(
        "s1",
        Path("simple_routing.p4"),
        grpc_port=60000,
        thrift_port=60000 if same_switch else 60001,
    )
    topo.add_link("h1", "s1")
    if not same_switch:
        topo.add_switch("s2", Path("simple_routing.p4"), grpc_port=60001, thrift_port=60002)
        topo.add_link("s1", "s2")
    net = Network(topo, log_dir=tmp_path / "logs")
    try:
        with pytest.raises(TopologyError, match="service port collision"):
            if asynchronous:
                await net.astart()
            else:
                net.start()
        patched["compiler"].compile.assert_not_called()
        for factory in ("ns_factory", "veth_factory", "bmv2_factory", "client_factory"):
            patched[factory].assert_not_called()
        assert not (tmp_path / "logs").exists()
        assert not net.is_running
    finally:
        await net.astop()


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
        # Replace each running host's namespace.popen with a controllable mock.
        h1 = net.host("h1")
        h2 = net.host("h2")
        h1.namespace.popen = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
        process = h1.namespace.popen.return_value
        process.__enter__.return_value = process
        process.wait.return_value = 0
        # Ping by name → uses h2's primary_ip.
        ok = net.ping("h1", "h2", count=2, timeout=1.0)
        assert ok is True
        argv = h1.namespace.popen.call_args.args[0]
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
        h1.namespace.popen = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
        process = h1.namespace.popen.return_value
        process.__enter__.return_value = process
        process.wait.return_value = 0
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
        h1.namespace.popen = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
        process = h1.namespace.popen.return_value
        process.__enter__.return_value = process
        process.wait.return_value = 1
        # An IP literal that's not a known host name is passed through.
        result = net.ping("h1", "203.0.113.5")
        assert result is False
        argv = h1.namespace.popen.call_args.args[0]
        assert argv[-1] == "203.0.113.5"
    finally:
        net.stop()


def test_pingall_returns_pair_results(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    try:
        for name in ("h1", "h2"):
            h = net.host(name)
            h.namespace.popen = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
            process = h.namespace.popen.return_value
            process.__enter__.return_value = process
            process.wait.return_value = 0
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
        h1.namespace.popen = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
        process = h1.namespace.popen.return_value
        process.__enter__.return_value = process
        process.wait.return_value = 0
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
        # Reset every host's namespace.popen so we only count pingall6's calls.
        for h_name in ("h1", "h2", "h3"):
            h = net.host(h_name)
            h.namespace.popen = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
            process = h.namespace.popen.return_value
            process.__enter__.return_value = process
            process.wait.return_value = 0
        result = net.pingall6()
        # Two ordered pairs over {h1, h2}: (h1,h2) and (h2,h1). h3 absent.
        assert set(result.keys()) == {("h1", "h2"), ("h2", "h1")}
        # Verify a real call: h1 used force_ipv6=True via the `-6` argv flag.
        argv = net.host("h1").namespace.popen.call_args.args[0]
        assert "-6" in argv
        # h3 received no pingall6-driven exec.
        net.host("h3").namespace.popen.assert_not_called()
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


class _LoopBoundClient:
    def __init__(self, *, connected: bool = True) -> None:
        self._loop = asyncio.get_running_loop()
        self.is_connected = connected
        self.disconnect_started = asyncio.Event()
        self.allow_disconnect = asyncio.Event()
        self.allow_disconnect.set()
        self.closed = False
        self.disconnect_count = 0

    async def disconnect(self) -> None:
        assert asyncio.get_running_loop() is self._loop
        self.disconnect_started.set()
        await self.allow_disconnect.wait()
        self.disconnect_count += 1
        self.is_connected = False
        self.closed = True


@pytest.mark.parametrize("mode", ["async", "worker", "owner"])
@pytest.mark.parametrize("connected", [True, False])
async def test_network_closes_async_client_on_owner_loop(
    patched: dict[str, Any], tmp_path: Path, mode: str, connected: bool
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    sw = net.switch("s1")
    client = _LoopBoundClient(connected=connected)
    sw._async_client = client  # type: ignore[assignment]
    try:
        if mode == "worker":
            await asyncio.to_thread(net.stop)
        elif mode == "owner":
            net.stop()
            await net.astop()
        else:
            await net.astop()
        assert client.closed
        assert client.disconnect_count == 1
        assert sw._async_client is None
        assert not net.is_running
        patched["clients"][0].disconnect.assert_called_once()
        patched["bmv2s"][0].stop.assert_called_once()
        for veth in patched["veths"]:
            veth.destroy.assert_called_once()
        await net.astop()
        net.stop()
        assert client.disconnect_count == 1
    finally:
        await client.disconnect()
        net.stop()


async def test_astop_waits_for_client_before_stopping_switch(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    client = _LoopBoundClient()
    client.allow_disconnect.clear()
    net.switch("s1")._async_client = client  # type: ignore[assignment]
    task = asyncio.create_task(net.astop())
    try:
        await asyncio.wait_for(client.disconnect_started.wait(), timeout=2.0)
        assert not task.done()
        patched["bmv2s"][0].stop.assert_not_called()
        client.allow_disconnect.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert client.closed
        patched["bmv2s"][0].stop.assert_called_once()
    finally:
        client.allow_disconnect.set()
        await asyncio.gather(task, return_exceptions=True)
        net.stop()


async def test_cancelled_astop_finishes_cleanup(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    client = _LoopBoundClient()
    client.allow_disconnect.clear()
    net.switch("s1")._async_client = client  # type: ignore[assignment]
    task = asyncio.create_task(net.astop())
    try:
        await asyncio.wait_for(client.disconnect_started.wait(), timeout=2.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        client.allow_disconnect.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        assert client.closed
        assert not net.is_running
        patched["bmv2s"][0].stop.assert_called_once()
    finally:
        client.allow_disconnect.set()
        await asyncio.gather(task, return_exceptions=True)
        net.stop()


def test_stop_closes_client_owned_by_background_loop(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()

    async def make_client() -> _LoopBoundClient:
        return _LoopBoundClient()

    client = asyncio.run_coroutine_threadsafe(make_client(), loop).result(timeout=2.0)
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    try:
        net.start()
        net.switch("s1")._async_client = client  # type: ignore[assignment]
        net.stop()
        assert client.closed
        assert client.disconnect_count == 1
        assert not net.is_running
    finally:
        net.stop()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        loop.close()


async def test_astop_continues_cleanup_after_client_error(
    patched: dict[str, Any], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class FailingClient(_LoopBoundClient):
        async def disconnect(self) -> None:
            assert asyncio.get_running_loop() is self._loop
            raise RuntimeError("disconnect failed")

    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    net.switch("s1")._async_client = FailingClient()  # type: ignore[assignment]
    await net.astop()
    assert "disconnect failed" in caplog.text
    assert not net.is_running
    patched["clients"][0].disconnect.assert_called_once()
    patched["bmv2s"][0].stop.assert_called_once()
    patched["unregister"].assert_called_once_with(net)


async def test_concurrent_astop_only_cleans_up_once(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    client = _LoopBoundClient()
    net.switch("s1")._async_client = client  # type: ignore[assignment]
    await asyncio.gather(net.astop(), net.astop(), net.astop())
    assert client.closed
    assert client.disconnect_count == 1
    patched["bmv2s"][0].stop.assert_called_once()
    patched["unregister"].assert_called_once_with(net)


def test_stop_allows_reentrant_cleanup(patched: dict[str, Any], tmp_path: Path) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    net.start()
    patched["clients"][0].disconnect.side_effect = net.stop
    net.stop()
    assert not net.is_running
    patched["clients"][0].disconnect.assert_called_once()
    patched["bmv2s"][0].stop.assert_called_once()


async def test_start_after_astop_can_be_stopped_again(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    for _ in range(2):
        net.start()
        client = _LoopBoundClient()
        net.switch("s1")._async_client = client  # type: ignore[assignment]
        await net.astop()
        assert client.closed
        assert not net.is_running
    for bmv2 in patched["bmv2s"]:
        bmv2.stop.assert_called_once()


def _pause_startup(
    patched: dict[str, Any], stage: str, *, fail: bool = False
) -> tuple[asyncio.Event, threading.Event]:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def pause() -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5), "startup was not released"
        if fail:
            raise RuntimeError("startup failed")

    if stage == "compile":
        compile_source = patched["compiler"].compile.side_effect

        def compile_paused(*args: Any, **kwargs: Any) -> Any:
            pause()
            return compile_source(*args, **kwargs)

        patched["compiler"].compile.side_effect = compile_paused
    else:
        make_ns = patched["ns_factory"].side_effect

        def make_paused_ns(name: str) -> MagicMock:
            ns = make_ns(name)
            ns.exec.side_effect = lambda argv: pause()
            return ns

        patched["ns_factory"].side_effect = make_paused_ns
    return entered, release


async def test_astart_installs_handlers_on_caller_and_runs_startup_in_worker(
    patched: dict[str, Any], tmp_path: Path
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    caller = threading.get_ident()
    threads: list[int] = []
    patched["install_handlers"].side_effect = lambda: threads.append(threading.get_ident())
    entered, release = _pause_startup(patched, "compile")
    startup = asyncio.create_task(net.astart())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert threads == [caller]
        patched["register"].assert_called_once_with(net)
        assert not net.is_running
        release.set()
        await asyncio.wait_for(startup, timeout=2)
        assert net.is_running
        assert net.host("h1").name == "h1"
        await net.astop()
        await net.astart()
        assert threads == [caller, caller]
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)
        await net.astop()


@pytest.mark.parametrize("stage", ["compile", "namespace"])
@pytest.mark.parametrize("fail", [False, True])
async def test_cancelled_astart_waits_for_startup_and_rollback(
    patched: dict[str, Any], tmp_path: Path, stage: str, fail: bool
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    entered, release = _pause_startup(patched, stage, fail=fail)
    startup = asyncio.create_task(net.astart())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        for _ in range(2):
            startup.cancel()
            await asyncio.sleep(0)
            assert not startup.done()
        namespaces = list(net._namespaces.values())
        for ns in namespaces:
            ns.destroy.assert_not_called()
        release.set()
        error = RuntimeError if fail else asyncio.CancelledError
        with pytest.raises(error):
            await asyncio.wait_for(startup, timeout=2)
        assert not net.is_running
        assert not net._namespaces
        assert not net._veth_pairs
        assert not net._bmv2_switches
        patched["unregister"].assert_called_once_with(net)
        for ns in namespaces:
            ns.destroy.assert_called_once()
        for bmv2 in patched["bmv2s"]:
            bmv2.stop.assert_called_once()
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)
        await net.astop()


@pytest.mark.parametrize("entry", ["sync", "async"])
@pytest.mark.parametrize("stop_mode", ["stop", "astop"])
async def test_shutdown_during_startup_waits_and_prevents_running_state(
    patched: dict[str, Any], tmp_path: Path, entry: str, stop_mode: str
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    entered, release = _pause_startup(patched, "namespace")
    startup = asyncio.create_task(
        net.astart() if entry == "async" else asyncio.to_thread(net.start)
    )
    shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        namespaces = list(net._namespaces.values())
        if stop_mode == "stop":
            net.stop()
        shutdown = asyncio.create_task(net.astop())

        async def wait_for_stop_request() -> None:
            while net._stop_future is None:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_stop_request(), timeout=2)
        assert not shutdown.done()
        for ns in namespaces:
            ns.destroy.assert_not_called()
        with pytest.raises(NetworkAlreadyRunningError):
            await net.astart()
        release.set()
        with pytest.raises(NetworkError, match="interrupted by shutdown"):
            await asyncio.wait_for(startup, timeout=2)
        await asyncio.wait_for(shutdown, timeout=2)
        assert not net.is_running
        assert not net._namespaces
        patched["unregister"].assert_called_once_with(net)
        for ns in namespaces:
            ns.destroy.assert_called_once()
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)
        if shutdown is not None:
            await shutdown
        await net.astop()


@pytest.mark.parametrize("stop_mode", ["stop", "astop"])
async def test_shutdown_before_startup_worker_runs(
    patched: dict[str, Any], tmp_path: Path, stop_mode: str
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    release = threading.Event()
    blocked = loop.run_in_executor(None, release.wait)
    startup = asyncio.create_task(net.astart())
    shutdown = None
    try:
        await asyncio.sleep(0)
        patched["register"].assert_called_once_with(net)
        if stop_mode == "stop":
            net.stop()
        shutdown = asyncio.create_task(net.astop())
        await asyncio.sleep(0)
        assert net._stop_future is not None
        assert not shutdown.done()
        release.set()
        with pytest.raises(NetworkError, match="interrupted by shutdown"):
            await asyncio.wait_for(startup, timeout=2)
        await asyncio.wait_for(shutdown, timeout=2)
        patched["ns_factory"].assert_not_called()
        patched["compiler"].compile.assert_not_called()
        patched["unregister"].assert_called_once_with(net)
        assert not net.is_running
    finally:
        release.set()
        await blocked
        await asyncio.gather(startup, return_exceptions=True)
        if shutdown is not None:
            await shutdown
        await net.astop()
        executor.shutdown()


async def test_astart_executor_failure_unregisters_and_allows_retry(
    patched: dict[str, Any], tmp_path: Path, mocker: MockerFixture
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    loop = asyncio.get_running_loop()
    submit = mocker.patch.object(
        loop, "run_in_executor", side_effect=RuntimeError("executor closed")
    )
    with pytest.raises(RuntimeError, match="executor closed"):
        await net.astart()
    patched["unregister"].assert_called_once_with(net)
    assert not net.is_running
    mocker.stop(submit)
    await net.astart()
    await net.astop()


@pytest.mark.parametrize("initial", ["start", "astart"])
async def test_duplicate_astart_preserves_running_network(
    patched: dict[str, Any], tmp_path: Path, initial: str
) -> None:
    net = Network(_make_simple_topology(), log_dir=tmp_path / "logs")
    if initial == "start":
        net.start()
    else:
        await net.astart()
    try:
        with pytest.raises(NetworkAlreadyRunningError):
            await net.astart()
        assert net.is_running
        patched["unregister"].assert_not_called()
        for bmv2 in patched["bmv2s"]:
            bmv2.stop.assert_not_called()
    finally:
        await net.astop()


_IPV6_QUERY = ["ip", "-j", "-6", "addr", "show"]


def _mock_ipv6_status(patched: dict[str, Any], query: Any) -> None:
    make_ns = patched["ns_factory"].side_effect

    def make_status_ns(name: str) -> MagicMock:
        namespace = make_ns(name)
        execute = namespace.exec.side_effect

        def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if argv == _IPV6_QUERY:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps(query(name)).encode(), stderr=b""
                )
            return execute(argv, **kwargs)

        namespace.exec.side_effect = run
        return namespace

    patched["ns_factory"].side_effect = make_status_ns


def _ipv6_state(host: str = "h1", address: str = "fd00::1", **flags: bool) -> list[dict[str, Any]]:
    return [{"ifname": f"{host}-eth0", "addr_info": [{"local": address, **flags}]}]


def _mock_ipv6_clock(mocker: MockerFixture) -> SimpleNamespace:
    now = [0.0]

    def advance(seconds: float) -> None:
        now[0] += seconds

    clock = SimpleNamespace(
        monotonic=lambda: now[0], sleep=MagicMock(side_effect=advance), time_ns=time.time_ns
    )
    mocker.patch("p4net.network.orchestrator.time", clock)
    return clock


def test_ipv4_start_skips_ipv6_status(patched: dict[str, Any], mocker: MockerFixture) -> None:
    clock = _mock_ipv6_clock(mocker)
    query = MagicMock(side_effect=AssertionError("IPv4 startup queried IPv6"))
    _mock_ipv6_status(patched, query)
    with Network(_make_simple_topology()):
        query.assert_not_called()
        clock.sleep.assert_not_called()


def test_start_waits_for_address_after_pipeline_setup(
    patched: dict[str, Any], mocker: MockerFixture
) -> None:
    clock = _mock_ipv6_clock(mocker)
    states = iter([[], _ipv6_state(tentative=True), _ipv6_state()])
    network = Network(_make_v6_only_topology())

    def query(name: str) -> list[dict[str, Any]]:
        assert not network.is_running
        assert not network.hosts
        patched["clients"][0].set_pipeline_config.assert_called_once()
        return next(states)

    _mock_ipv6_status(patched, query)
    try:
        network.start()
        assert network.is_running
        assert clock.monotonic() > 0
    finally:
        network.stop()


def test_ipv6_readiness_uses_resolved_interfaces_and_addresses(
    patched: dict[str, Any], mocker: MockerFixture
) -> None:
    _mock_ipv6_clock(mocker)
    topology = Topology()
    topology.add_host("h1", ip6="fd00::1/64")
    topology.add_switch("s1", Path("first.p4"))
    topology.add_switch("s2", Path("second.p4"))
    topology.add_link("h1", "s1", ip6_a="fd00:38::a/64")
    topology.add_link("h1", "s2", ip6_a="fd00:39::b/64")
    query = MagicMock(
        return_value=[
            {
                "ifname": "h1-eth0",
                "addr_info": [
                    {"local": "fe80::bad", "dadfailed": True},
                    {"local": "fd00:0038:0:0:0:0:0:000a"},
                ],
            },
            {"ifname": "h1-eth1", "addr_info": [{"local": "FD00:39::B"}]},
        ]
    )
    _mock_ipv6_status(patched, query)
    with Network(topology) as network:
        assert network.is_running
        query.assert_called_once_with("h1")


def test_ipv6_dad_failure_rolls_back_all_resources(patched: dict[str, Any]) -> None:
    _mock_ipv6_status(patched, lambda name: _ipv6_state(dadfailed=True, tentative=True))
    network = Network(_make_v6_only_topology())
    with pytest.raises(NetworkError, match="duplicate address detection") as error:
        network.start()
    assert "h1/h1-eth0" in str(error.value) and "fd00::1" in str(error.value)
    assert not network.is_running and not network.hosts and not network.switches
    assert not network._namespaces and not network._veth_pairs
    patched["clients"][0].disconnect.assert_called_once()
    patched["bmv2s"][0].stop.assert_called_once()
    patched["veths"][0].destroy.assert_called_once()
    patched["unregister"].assert_called_once_with(network)


@pytest.mark.parametrize("state", ["missing", "tentative", "wrong-interface", "wrong-address"])
def test_ipv6_unready_address_times_out_and_rolls_back(
    patched: dict[str, Any], mocker: MockerFixture, state: str
) -> None:
    _mock_ipv6_clock(mocker)
    mocker.patch("p4net.network.orchestrator._IPV6_READY_TIMEOUT", 0.1)
    records = _ipv6_state(tentative=True)
    if state == "missing":
        records = []
    elif state == "wrong-interface":
        records = [{"ifname": "lo", "addr_info": [{"local": "fd00::1"}]}]
    elif state == "wrong-address":
        records = _ipv6_state(address="fd00::2")
    _mock_ipv6_status(patched, lambda name: records)
    network = Network(_make_v6_only_topology())
    with pytest.raises(NetworkError, match="IPv6 addresses not ready") as error:
        network.start()
    assert "h1/h1-eth0" in str(error.value) and "fd00::1" in str(error.value)
    assert not network.is_running and not network._namespaces and not network._veth_pairs
    patched["bmv2s"][0].stop.assert_called_once()


def test_ipv6_status_command_timeout_rolls_back(patched: dict[str, Any]) -> None:
    query = MagicMock(side_effect=subprocess.TimeoutExpired(_IPV6_QUERY, 5))
    _mock_ipv6_status(patched, query)
    network = Network(_make_v6_only_topology())
    with pytest.raises(NetworkError, match="IPv6 addresses not ready") as error:
        network.start()
    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)
    assert not network.is_running and not network._namespaces


def test_ipv6_hosts_share_one_wait_budget(patched: dict[str, Any], mocker: MockerFixture) -> None:
    clock = _mock_ipv6_clock(mocker)
    mocker.patch("p4net.network.orchestrator._IPV6_READY_TIMEOUT", 0.1)
    topology = Topology()
    for name, address in (("h1", "fd00::1/64"), ("h2", "fd00::2/64")):
        topology.add_host(name, ip6=address)
    topology.add_link("h1", "h2")
    network = Network(topology)

    def query(name: str) -> list[dict[str, Any]]:
        remaining = network._namespaces[name].exec.call_args.kwargs["timeout"]
        if remaining < 0.08:
            raise subprocess.TimeoutExpired(_IPV6_QUERY, remaining)
        clock.sleep(0.08)
        return _ipv6_state(name, "fd00::1" if name == "h1" else "fd00::2")

    _mock_ipv6_status(patched, query)
    with pytest.raises(NetworkError, match="h2/h2-eth0"):
        network.start()
    assert not network.is_running and not network._namespaces


@pytest.mark.parametrize("cancel", [False, True])
async def test_ipv6_wait_cancellation_and_shutdown_clean_up(
    patched: dict[str, Any], cancel: bool
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def query(name: str) -> list[dict[str, Any]]:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        return _ipv6_state(tentative=not cancel)

    _mock_ipv6_status(patched, query)
    network = Network(_make_v6_only_topology())
    startup = asyncio.create_task(network.astart())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if cancel:
            startup.cancel()
        else:
            network.stop()
        await asyncio.sleep(0)
        assert not network.is_running
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel else NetworkError):
            await asyncio.wait_for(startup, timeout=2)
        await network.astop()
        assert not network._namespaces and not network._veth_pairs
        patched["clients"][0].disconnect.assert_called_once()
        patched["bmv2s"][0].stop.assert_called_once()
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)
        await network.astop()
