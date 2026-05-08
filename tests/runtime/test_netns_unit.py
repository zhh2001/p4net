"""Unit tests for `p4net.runtime.netns` (no privilege; pyroute2 mocked)."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from p4net.runtime import NamespaceError, NetworkNamespace, NSProcess


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        " ",
        "has space",
        "has\ttab",
        "has\nnewline",
        "with/slash",
        "..",
        "a/../b",
        "x" * 33,
    ],
)
def test_invalid_name_rejected(bad_name: str) -> None:
    with pytest.raises(ValueError):
        NetworkNamespace(bad_name)


def test_valid_name_accepted() -> None:
    ns = NetworkNamespace("p4net_test")
    assert ns.name == "p4net_test"
    assert "p4net_test" in repr(ns)


@pytest.fixture
def mock_netns(mocker: MockerFixture) -> MagicMock:
    fake = mocker.patch("p4net.runtime.netns._netns")
    fake.listnetns.return_value = []
    return fake


def test_exists_reflects_listnetns(mock_netns: MagicMock) -> None:
    ns = NetworkNamespace("alpha")
    assert ns.exists is False
    mock_netns.listnetns.return_value = ["alpha", "beta"]
    assert ns.exists is True


def test_create_calls_pyroute2(mock_netns: MagicMock) -> None:
    ns = NetworkNamespace("alpha")
    ns.create()
    mock_netns.create.assert_called_once_with("alpha")


def test_create_raises_when_already_exists(mock_netns: MagicMock) -> None:
    mock_netns.listnetns.return_value = ["alpha"]
    ns = NetworkNamespace("alpha")
    with pytest.raises(NamespaceError):
        ns.create()
    mock_netns.create.assert_not_called()


def test_destroy_calls_pyroute2(mock_netns: MagicMock) -> None:
    mock_netns.listnetns.return_value = ["alpha"]
    ns = NetworkNamespace("alpha")
    ns.destroy()
    mock_netns.remove.assert_called_once_with("alpha")


def test_destroy_raises_when_missing(mock_netns: MagicMock) -> None:
    ns = NetworkNamespace("alpha")
    with pytest.raises(NamespaceError):
        ns.destroy()
    mock_netns.remove.assert_not_called()


def test_context_manager_creates_and_destroys(mock_netns: MagicMock) -> None:
    listed: list[list[str]] = [[], ["alpha"], ["alpha"], []]

    def listnetns() -> list[str]:
        return listed.pop(0) if listed else []

    mock_netns.listnetns.side_effect = listnetns
    with NetworkNamespace("alpha") as ns:
        assert ns.name == "alpha"
    mock_netns.create.assert_called_once_with("alpha")
    mock_netns.remove.assert_called_once_with("alpha")


def test_context_manager_skips_destroy_if_already_gone(mock_netns: MagicMock) -> None:
    # create() sees an empty list (so it succeeds); __exit__ sees an empty
    # list again (so destroy is NOT invoked).
    states = iter([[], []])

    def listnetns() -> list[str]:
        return next(states)

    mock_netns.listnetns.side_effect = listnetns
    with NetworkNamespace("alpha"):
        pass
    mock_netns.remove.assert_not_called()


@pytest.fixture
def mock_nspopen(mocker: MockerFixture) -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate.return_value = (b"", b"")
    factory = mocker.patch("p4net.runtime.netns.NSPopen", return_value=proc)
    factory.proc = proc  # type: ignore[attr-defined]
    return factory


def test_exec_returns_completed_process(mock_nspopen: MagicMock) -> None:
    proc: Any = mock_nspopen.proc
    proc.returncode = 0
    proc.communicate.return_value = (b"out", b"err")
    ns = NetworkNamespace("alpha")
    result = ns.exec(["true"], capture_output=True)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert result.stdout == b"out"
    assert result.stderr == b"err"
    proc.release.assert_called_once()


def test_exec_check_raises_on_nonzero(mock_nspopen: MagicMock) -> None:
    proc: Any = mock_nspopen.proc
    proc.returncode = 7
    proc.communicate.return_value = (b"", b"boom")
    ns = NetworkNamespace("alpha")
    with pytest.raises(subprocess.CalledProcessError) as info:
        ns.exec(["false"], capture_output=True)
    assert info.value.returncode == 7
    proc.release.assert_called_once()


def test_exec_check_false_returns_nonzero(mock_nspopen: MagicMock) -> None:
    proc: Any = mock_nspopen.proc
    proc.returncode = 1
    proc.communicate.return_value = (b"", b"")
    ns = NetworkNamespace("alpha")
    result = ns.exec(["false"], check=False)
    assert result.returncode == 1


def test_exec_timeout_kills_process(mock_nspopen: MagicMock) -> None:
    proc: Any = mock_nspopen.proc
    proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=0.1)
    ns = NetworkNamespace("alpha")
    with pytest.raises(subprocess.TimeoutExpired):
        ns.exec(["sleep", "5"], timeout=0.1)
    proc.kill.assert_called_once()
    proc.release.assert_called_once()


def test_popen_returns_nsprocess(mock_nspopen: MagicMock) -> None:
    proc: Any = mock_nspopen.proc
    proc.pid = 1234
    proc.poll.return_value = None
    ns = NetworkNamespace("alpha")
    handle = ns.popen(["sleep", "60"])
    assert isinstance(handle, NSProcess)
    assert handle.pid == 1234
    mock_nspopen.assert_called_once()


def test_nsprocess_forwards_methods(mock_nspopen: MagicMock) -> None:
    proc: Any = mock_nspopen.proc
    proc.pid = 7777
    proc.poll.return_value = 0
    proc.wait.return_value = 0
    ns = NetworkNamespace("alpha")
    handle = ns.popen(["true"])
    assert handle.poll() == 0
    assert handle.wait(timeout=1.0) == 0
    handle.terminate()
    proc.terminate.assert_called_once()
    handle.kill()
    proc.kill.assert_called_once()


def test_nsprocess_close_is_idempotent() -> None:
    fake = MagicMock()
    np = NSProcess(fake)
    np.close()
    np.close()
    np.close()
    fake.release.assert_called_once()


def test_nsprocess_close_swallows_release_errors() -> None:
    fake = MagicMock()
    fake.release.side_effect = RuntimeError("boom")
    np = NSProcess(fake)
    # Must not raise.
    np.close()
    fake.release.assert_called_once()


def test_nsprocess_context_manager_terminates_running_process() -> None:
    fake = MagicMock()
    # Process appears alive on first poll, then dies.
    fake.poll.side_effect = [None, 0, 0]
    with NSProcess(fake):
        pass
    fake.terminate.assert_called_once()
    fake.wait.assert_called_with(timeout=5)
    fake.release.assert_called_once()


def test_nsprocess_context_manager_kills_stubborn_process() -> None:
    fake = MagicMock()
    # Stays alive even after terminate+wait, then dies after kill.
    fake.poll.side_effect = [None, None, 0]
    with NSProcess(fake):
        pass
    fake.terminate.assert_called_once()
    fake.kill.assert_called_once()
    fake.release.assert_called_once()


def test_nsprocess_context_manager_skips_shutdown_if_already_exited() -> None:
    fake = MagicMock()
    fake.poll.return_value = 0
    with NSProcess(fake):
        pass
    fake.terminate.assert_not_called()
    fake.kill.assert_not_called()
    fake.release.assert_called_once()


def test_nsprocess_context_manager_does_not_suppress_body_exception() -> None:
    fake = MagicMock()
    fake.poll.return_value = 0
    fake.release.side_effect = RuntimeError("release failure")
    with pytest.raises(ValueError, match="from body"), NSProcess(fake):
        raise ValueError("from body")
    fake.release.assert_called_once()


def test_nsprocess_del_does_not_raise() -> None:
    fake = MagicMock()
    fake.release.side_effect = RuntimeError("destroy failure")
    np = NSProcess(fake)
    # Trigger __del__ explicitly; it must not raise even though release fails.
    np.__del__()
