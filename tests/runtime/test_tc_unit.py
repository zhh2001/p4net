"""Unit tests for `p4net.runtime.tc` (no privilege; subprocess mocked)."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from p4net.runtime import TcError, apply_netem, clear_qdisc


@pytest.fixture
def fake_run(mocker: MockerFixture) -> MagicMock:
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
    return mocker.patch("p4net.runtime.tc.subprocess.run", return_value=proc)


def _make_proc(rc: int, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=b"", stderr=stderr)


def test_apply_netem_requires_at_least_one_arg() -> None:
    with pytest.raises(TcError):
        apply_netem(None, "veth0")


def test_apply_netem_jitter_requires_delay() -> None:
    with pytest.raises(TcError):
        apply_netem(None, "veth0", jitter="2ms")


@pytest.mark.parametrize("bad_rate", ["fast", "10", "10mbps/s", "abc bit", ""])
def test_apply_netem_validates_rate(bad_rate: str) -> None:
    with pytest.raises(TcError):
        apply_netem(None, "veth0", rate=bad_rate)


@pytest.mark.parametrize("good_rate", ["10mbit", "100kbit", "1gbit", "500bps", "2.5mbit"])
def test_apply_netem_accepts_valid_rates(fake_run: MagicMock, good_rate: str) -> None:
    apply_netem(None, "veth0", rate=good_rate)


@pytest.mark.parametrize("bad_delay", ["10", "10msec", "abc", ""])
def test_apply_netem_validates_delay(bad_delay: str) -> None:
    with pytest.raises(TcError):
        apply_netem(None, "veth0", delay=bad_delay)


@pytest.mark.parametrize("bad_jitter", ["10", "abc", ""])
def test_apply_netem_validates_jitter(bad_jitter: str) -> None:
    with pytest.raises(TcError):
        apply_netem(None, "veth0", delay="10ms", jitter=bad_jitter)


@pytest.mark.parametrize("bad_loss", [-0.1, 100.1, -1.0, 200.0])
def test_apply_netem_validates_loss(bad_loss: float) -> None:
    with pytest.raises(TcError):
        apply_netem(None, "veth0", loss_pct=bad_loss)


def test_apply_netem_argv_with_loss(fake_run: MagicMock) -> None:
    apply_netem(None, "veth0", loss_pct=25.0)
    argv = fake_run.call_args.args[0]
    assert argv[:7] == ["tc", "qdisc", "replace", "dev", "veth0", "root", "netem"]
    assert "loss" in argv
    assert "25.0%" in argv


def test_apply_netem_argv_with_delay_and_jitter(fake_run: MagicMock) -> None:
    apply_netem(None, "veth0", delay="10ms", jitter="2ms")
    argv = fake_run.call_args.args[0]
    assert "delay" in argv
    delay_idx = argv.index("delay")
    assert argv[delay_idx + 1] == "10ms"
    assert argv[delay_idx + 2] == "2ms"


def test_apply_netem_argv_with_rate(fake_run: MagicMock) -> None:
    apply_netem(None, "veth0", rate="10mbit")
    argv = fake_run.call_args.args[0]
    assert "rate" in argv
    assert "10mbit" in argv


def test_apply_netem_uses_namespace_exec(mocker: MockerFixture) -> None:
    fake_ns: Any = MagicMock()
    fake_ns.name = "ns0"
    fake_ns.exec.return_value = _make_proc(0)
    mocker.patch("p4net.runtime.tc.subprocess.run")
    apply_netem(fake_ns, "veth0", loss_pct=10.0)
    fake_ns.exec.assert_called_once()
    argv = fake_ns.exec.call_args.args[0]
    assert argv[0] == "tc"


def test_apply_netem_raises_on_failure(fake_run: MagicMock) -> None:
    fake_run.return_value = _make_proc(2, stderr=b"RTNETLINK answers: Operation not permitted")
    with pytest.raises(TcError) as info:
        apply_netem(None, "veth0", loss_pct=10.0)
    assert "rc=2" in str(info.value)


def test_clear_qdisc_success(fake_run: MagicMock) -> None:
    clear_qdisc(None, "veth0")
    argv = fake_run.call_args.args[0]
    assert argv == ["tc", "qdisc", "del", "dev", "veth0", "root"]


def test_clear_qdisc_idempotent_on_no_such_file(fake_run: MagicMock) -> None:
    fake_run.return_value = _make_proc(2, stderr=b"RTNETLINK answers: No such file or directory")
    clear_qdisc(None, "veth0")  # must not raise


def test_clear_qdisc_idempotent_on_no_qdisc(fake_run: MagicMock) -> None:
    fake_run.return_value = _make_proc(2, stderr=b"Error: Cannot find specified qdisc.")
    clear_qdisc(None, "veth0")  # must not raise


def test_clear_qdisc_idempotent_on_handle_of_zero(fake_run: MagicMock) -> None:
    fake_run.return_value = _make_proc(2, stderr=b"Error: Cannot delete qdisc with handle of zero.")
    clear_qdisc(None, "veth0")  # must not raise


def test_clear_qdisc_raises_on_other_error(fake_run: MagicMock) -> None:
    fake_run.return_value = _make_proc(1, stderr=b"some other failure")
    with pytest.raises(TcError):
        clear_qdisc(None, "veth0")


def test_clear_qdisc_uses_namespace_exec() -> None:
    fake_ns: Any = MagicMock()
    fake_ns.name = "ns0"
    fake_ns.exec.return_value = _make_proc(0)
    clear_qdisc(fake_ns, "veth0")
    fake_ns.exec.assert_called_once()
    argv = fake_ns.exec.call_args.args[0]
    assert argv[:3] == ["tc", "qdisc", "del"]
