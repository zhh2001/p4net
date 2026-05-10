"""Unit tests for `p4net.network.nodes`. All primitives mocked."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from p4net.network import NetworkError, RunningHost, RunningSwitch
from p4net.topo import Host, P4Switch

# ---------------------------------------------------------------------------
# RunningHost
# ---------------------------------------------------------------------------


def _make_host(name: str = "h1", **kwargs: object) -> RunningHost:
    h = Host(name=name, **kwargs)  # type: ignore[arg-type]
    ns = MagicMock(name="NetworkNamespace")
    ifaces = {"h1-eth0": "10.0.0.1/24"}
    return RunningHost(h, ns, ifaces)


def test_running_host_basic_properties() -> None:
    rh = _make_host()
    assert rh.name == "h1"
    assert rh.primary_ip == "10.0.0.1"
    assert rh.namespace is not None
    assert rh.interfaces == {"h1-eth0": "10.0.0.1/24"}
    assert "h1" in repr(rh)


def test_running_host_primary_ip_none_when_no_iface() -> None:
    h = Host(name="h0")
    ns = MagicMock()
    rh = RunningHost(h, ns, {"h0-eth0": None})
    assert rh.primary_ip is None


def test_running_host_primary_ip_picks_first_with_ip() -> None:
    h = Host(name="h2")
    ns = MagicMock()
    rh = RunningHost(h, ns, {"h2-eth0": None, "h2-eth1": "192.168.1.2/24"})
    assert rh.primary_ip == "192.168.1.2"


def test_running_host_exec_delegates_to_namespace() -> None:
    rh = _make_host()
    rh.namespace.exec = MagicMock(  # type: ignore[method-assign]
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
    )
    rh.exec(["ls"], timeout=2.0, check=False, capture_output=True)
    rh.namespace.exec.assert_called_once_with(
        ["ls"], timeout=2.0, check=False, capture_output=True, env=None
    )


def test_running_host_popen_delegates_to_namespace() -> None:
    rh = _make_host()
    rh.namespace.popen = MagicMock(return_value=MagicMock(name="NSProcess"))  # type: ignore[method-assign]
    rh.popen(["sleep", "60"])
    rh.namespace.popen.assert_called_once()


def test_running_host_ping_string_returns_true_on_zero_rc() -> None:
    rh = _make_host()
    rh.namespace.exec = MagicMock(  # type: ignore[method-assign]
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
    )
    assert rh.ping("10.0.0.2", count=1, timeout=2.0) is True
    argv = rh.namespace.exec.call_args.args[0]
    # `-4` selects IPv4 explicitly. `-w` (overall deadline) is appended so
    # ping doesn't hang under heavy loss.
    assert argv[:6] == ["ping", "-4", "-c", "1", "-W", "2"]
    assert "-w" in argv
    assert argv[-1] == "10.0.0.2"


def test_running_host_ping_returns_false_on_nonzero_rc() -> None:
    rh = _make_host()
    rh.namespace.exec = MagicMock(  # type: ignore[method-assign]
        return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"")
    )
    assert rh.ping("10.0.0.99") is False


def test_running_host_ping_other_host_uses_primary_ip() -> None:
    rh1 = _make_host("h1")
    rh2 = _make_host("h2")
    # Make h2's primary_ip differ from h1's so we can verify it.
    rh2._interfaces["h1-eth0"] = "10.0.0.2/24"
    rh1.namespace.exec = MagicMock(  # type: ignore[method-assign]
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
    )
    rh1.ping(rh2)
    argv = rh1.namespace.exec.call_args.args[0]
    assert argv[-1] == "10.0.0.2"


def test_running_host_ping_other_host_without_ip_raises() -> None:
    rh1 = _make_host("h1")
    h2 = Host(name="h2")
    rh2 = RunningHost(h2, MagicMock(), {"h2-eth0": None})
    with pytest.raises(NetworkError, match="no primary IP"):
        rh1.ping(rh2)


# ---------------------------------------------------------------------------
# RunningSwitch
# ---------------------------------------------------------------------------


def test_running_switch_properties() -> None:
    sw = P4Switch(name="s1", p4_src=Path("p.p4"))
    bmv2 = MagicMock()
    bmv2.log_file = Path("/var/log/s1.log")
    bmv2.grpc_address = "127.0.0.1:50051"
    client = MagicMock()
    compile_result = MagicMock()
    rs = RunningSwitch(sw, bmv2, client, compile_result)
    assert rs.name == "s1"
    assert rs.descriptor is sw
    assert rs.bmv2 is bmv2
    assert rs.client is client
    assert rs.compile_result is compile_result
    assert rs.log_file == Path("/var/log/s1.log")
    assert "s1" in repr(rs)
