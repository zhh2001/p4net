"""Unit tests for `p4net.runtime.sysctl`."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from p4net.runtime.exceptions import NamespaceError
from p4net.runtime.sysctl import disable_ipv6, enable_ipv6


def _completed(rc: int = 0, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=b"", stderr=stderr)


def test_disable_ipv6_runs_expected_argv() -> None:
    ns = MagicMock()
    ns.exec.return_value = _completed()
    disable_ipv6(ns, "eth0")
    ns.exec.assert_called_once_with(
        ["sysctl", "-w", "net.ipv6.conf.eth0.disable_ipv6=1"],
        capture_output=True,
        check=False,
    )


def test_enable_ipv6_default_three_calls() -> None:
    ns = MagicMock()
    ns.exec.return_value = _completed()
    enable_ipv6(ns, "eth0")
    keys = [c.args[0][2] for c in ns.exec.call_args_list]
    assert keys == [
        "net.ipv6.conf.eth0.disable_ipv6=0",
        "net.ipv6.conf.eth0.accept_ra=0",
        "net.ipv6.conf.eth0.autoconf=0",
    ]


def test_enable_ipv6_accept_ra_true() -> None:
    ns = MagicMock()
    ns.exec.return_value = _completed()
    enable_ipv6(ns, "eth0", accept_ra=True)
    keys = [c.args[0][2] for c in ns.exec.call_args_list]
    assert keys == [
        "net.ipv6.conf.eth0.disable_ipv6=0",
        "net.ipv6.conf.eth0.accept_ra=1",
        "net.ipv6.conf.eth0.autoconf=0",
    ]


def test_enable_ipv6_autoconf_true() -> None:
    ns = MagicMock()
    ns.exec.return_value = _completed()
    enable_ipv6(ns, "eth0", accept_ra=True, autoconf=True)
    keys = [c.args[0][2] for c in ns.exec.call_args_list]
    assert keys == [
        "net.ipv6.conf.eth0.disable_ipv6=0",
        "net.ipv6.conf.eth0.accept_ra=1",
        "net.ipv6.conf.eth0.autoconf=1",
    ]


def test_disable_ipv6_root_namespace_uses_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run = MagicMock(return_value=_completed())
    monkeypatch.setattr(subprocess, "run", fake_run)
    disable_ipv6(None, "eth0")
    fake_run.assert_called_once_with(
        ["sysctl", "-w", "net.ipv6.conf.eth0.disable_ipv6=1"],
        capture_output=True,
        check=False,
    )


def test_failure_raises_namespace_error() -> None:
    ns = MagicMock()
    ns.exec.return_value = _completed(rc=1, stderr=b"sysctl: permission denied\n")
    with pytest.raises(NamespaceError, match="permission denied"):
        disable_ipv6(ns, "eth0")
