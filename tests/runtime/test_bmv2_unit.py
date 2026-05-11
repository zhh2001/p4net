"""Unit tests for `p4net.runtime.bmv2`. All subprocess + socket calls mocked."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from p4net.runtime import (
    BMv2NotFoundError,
    BMv2StartupError,
    BMv2Switch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_switch(
    tmp_path: Path,
    *,
    port_to_iface: dict[int, str] | None = None,
    pcap_dir: Path | None = None,
    cpu_port: int | None = None,
    log_level: str = "info",
    grpc_bind_addr: str = "127.0.0.1",
    binary: str = "simple_switch_grpc",
    grpc_port: int = 50051,
    thrift_port: int = 9090,
    device_id: int = 0,
    name: str = "s1",
    startup_timeout: float = 10.0,
) -> BMv2Switch:
    return BMv2Switch(
        name,
        device_id=device_id,
        grpc_port=grpc_port,
        thrift_port=thrift_port,
        bmv2_json=tmp_path / "program.json",
        port_to_iface=port_to_iface or {},
        log_dir=tmp_path / "logs",
        pcap_dir=pcap_dir,
        cpu_port=cpu_port,
        log_level=log_level,
        grpc_bind_addr=grpc_bind_addr,
        binary=binary,
        startup_timeout=startup_timeout,
    )


def _patch_which(mocker: MockerFixture, *, found: bool = True) -> MagicMock:
    return mocker.patch(
        "p4net.runtime.bmv2.shutil.which",
        return_value="/usr/local/bin/simple_switch_grpc" if found else None,
    )


def _patch_popen(mocker: MockerFixture, proc: MagicMock | None = None) -> MagicMock:
    if proc is None:
        proc = MagicMock()
        proc.pid = 12345
        proc.poll.return_value = None
        proc.returncode = None
    return mocker.patch(
        "p4net.runtime.bmv2.subprocess.Popen",
        return_value=proc,
    )


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_constructor_minimal_ok(tmp_path: Path) -> None:
    sw = _make_switch(tmp_path)
    assert sw.name == "s1"
    assert sw.grpc_address == "127.0.0.1:50051"
    assert sw.log_file == tmp_path / "logs" / "s1.log"
    assert sw.pid is None
    assert sw.is_running() is False
    assert sw.returncode() is None


@pytest.mark.parametrize("bad_level", ["", "FATAL", "info ", "verbose"])
def test_constructor_rejects_bad_log_level(tmp_path: Path, bad_level: str) -> None:
    with pytest.raises(ValueError, match="invalid log_level"):
        _make_switch(tmp_path, log_level=bad_level)


def test_constructor_rejects_empty_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        BMv2Switch(
            "",
            device_id=0,
            grpc_port=50051,
            thrift_port=9090,
            bmv2_json=tmp_path / "program.json",
            port_to_iface={},
            log_dir=tmp_path / "logs",
        )


@pytest.mark.parametrize("bad_timeout", [0, -0.5, -10.0])
def test_constructor_rejects_nonpositive_startup_timeout(
    tmp_path: Path, bad_timeout: float
) -> None:
    with pytest.raises(ValueError, match="positive"):
        _make_switch(tmp_path, startup_timeout=bad_timeout)


# ---------------------------------------------------------------------------
# Argv construction
# ---------------------------------------------------------------------------


def test_argv_includes_one_dash_i_per_port_in_numerical_order(tmp_path: Path) -> None:
    sw = _make_switch(
        tmp_path,
        port_to_iface={3: "s1-eth3", 1: "s1-eth1", 2: "s1-eth2"},
    )
    argv = sw._build_argv()
    # Find positions of '-i' and verify port ordering.
    indices = [i for i, tok in enumerate(argv) if tok == "-i"]
    assert len(indices) == 3
    assert argv[indices[0] + 1] == "1@s1-eth1"
    assert argv[indices[1] + 1] == "2@s1-eth2"
    assert argv[indices[2] + 1] == "3@s1-eth3"


def test_argv_pcap_only_when_pcap_dir_set(tmp_path: Path) -> None:
    sw_off = _make_switch(tmp_path)
    assert "--pcap" not in sw_off._build_argv()
    sw_on = _make_switch(tmp_path, pcap_dir=tmp_path / "pcaps")
    argv = sw_on._build_argv()
    idx = argv.index("--pcap")
    assert argv[idx + 1] == str(tmp_path / "pcaps")


def test_argv_cpu_port_only_when_set(tmp_path: Path) -> None:
    sw_off = _make_switch(tmp_path)
    assert "--cpu-port" not in sw_off._build_argv()
    sw_on = _make_switch(tmp_path, cpu_port=255)
    argv = sw_on._build_argv()
    # cpu-port lives AFTER the '--' separator.
    sep = argv.index("--")
    cpu_idx = argv.index("--cpu-port")
    assert cpu_idx > sep
    assert argv[cpu_idx + 1] == "255"


def test_argv_bmv2_json_is_before_separator(tmp_path: Path) -> None:
    """JSON config is a BMv2 core positional, so it must precede the `--`."""
    sw = _make_switch(tmp_path)
    argv = sw._build_argv()
    json_str = str(tmp_path / "program.json")
    assert json_str in argv
    sep = argv.index("--")
    assert argv.index(json_str) < sep


def test_argv_grpc_bind_addr_honored(tmp_path: Path) -> None:
    sw = _make_switch(tmp_path, grpc_bind_addr="0.0.0.0", grpc_port=60000)
    argv = sw._build_argv()
    idx = argv.index("--grpc-server-addr")
    assert argv[idx + 1] == "0.0.0.0:60000"


def test_argv_custom_binary_used(tmp_path: Path) -> None:
    sw = _make_switch(tmp_path, binary="my-bmv2")
    argv = sw._build_argv()
    assert argv[0] == "my-bmv2"


def test_argv_log_level_and_thrift_and_device_id(tmp_path: Path) -> None:
    sw = _make_switch(tmp_path, log_level="debug", thrift_port=9095, device_id=42)
    argv = sw._build_argv()
    assert argv[argv.index("-L") + 1] == "debug"
    assert argv[argv.index("--thrift-port") + 1] == "9095"
    assert argv[argv.index("--device-id") + 1] == "42"
    assert "--log-console" in argv
    assert "--log-flush" in argv


# ---------------------------------------------------------------------------
# start() / binary discovery / lifecycle
# ---------------------------------------------------------------------------


def test_start_raises_bmv2_not_found_when_binary_missing(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    _patch_which(mocker, found=False)
    sw = _make_switch(tmp_path)
    with pytest.raises(BMv2NotFoundError, match="not found on PATH"):
        sw.start()
    assert sw.pid is None
    assert sw.is_running() is False


def test_start_then_start_again_raises(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    _patch_popen(mocker)
    sw = _make_switch(tmp_path)
    sw.start()
    with pytest.raises(BMv2StartupError, match="already started"):
        sw.start()


def test_start_uses_absolute_binary_path(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    popen = _patch_popen(mocker)
    sw = _make_switch(tmp_path)
    sw.start()
    argv = popen.call_args.args[0]
    assert argv[0] == "/usr/local/bin/simple_switch_grpc"


def test_start_creates_log_dir_and_opens_file(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    _patch_popen(mocker)
    sw = _make_switch(tmp_path)
    sw.start()
    assert (tmp_path / "logs").is_dir()
    assert sw.log_file == tmp_path / "logs" / "s1.log"
    assert sw.log_file.exists()


def test_start_creates_pcap_dir(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    _patch_popen(mocker)
    sw = _make_switch(tmp_path, pcap_dir=tmp_path / "pcaps")
    sw.start()
    assert (tmp_path / "pcaps").is_dir()


def test_start_translates_oserror_to_startuperror(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    mocker.patch(
        "p4net.runtime.bmv2.subprocess.Popen",
        side_effect=OSError("argv is unhappy"),
    )
    sw = _make_switch(tmp_path)
    with pytest.raises(BMv2StartupError, match="failed to spawn"):
        sw.start()


def test_pid_reflects_popen_pid(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 55555
    proc.poll.return_value = None
    _patch_popen(mocker, proc=proc)
    sw = _make_switch(tmp_path)
    assert sw.pid is None
    sw.start()
    assert sw.pid == 55555


# ---------------------------------------------------------------------------
# wait_until_ready
# ---------------------------------------------------------------------------


def test_wait_until_ready_succeeds_on_socket_connect(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = None
    _patch_popen(mocker, proc=proc)
    fake_sock = MagicMock()
    fake_sock.__enter__.return_value = fake_sock
    fake_sock.__exit__.return_value = None
    mocker.patch(
        "p4net.runtime.bmv2.socket.create_connection",
        return_value=fake_sock,
    )
    sw = _make_switch(tmp_path)
    sw.start()
    sw.wait_until_ready(timeout=1.0)


def test_wait_until_ready_raises_when_process_exits(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = 7  # already exited with rc 7
    proc.returncode = 7
    _patch_popen(mocker, proc=proc)
    sw = _make_switch(tmp_path)
    sw.start()
    with pytest.raises(BMv2StartupError, match="returncode=7"):
        sw.wait_until_ready(timeout=1.0)


def test_wait_until_ready_raises_on_timeout(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = None  # stays alive forever
    _patch_popen(mocker, proc=proc)
    mocker.patch(
        "p4net.runtime.bmv2.socket.create_connection",
        side_effect=OSError("connection refused"),
    )
    # Make sleep instantaneous and time progression deterministic.
    mocker.patch("p4net.runtime.bmv2.time.sleep")
    times = iter([0.0, 0.05, 0.2, 1.0, 5.0])
    mocker.patch(
        "p4net.runtime.bmv2.time.monotonic",
        side_effect=lambda: next(times),
    )
    sw = _make_switch(tmp_path)
    sw.start()
    with pytest.raises(BMv2StartupError, match=r"0\.1s"):
        sw.wait_until_ready(timeout=0.1)


def test_wait_until_ready_requires_start_first(tmp_path: Path) -> None:
    sw = _make_switch(tmp_path)
    with pytest.raises(BMv2StartupError, match="has not been started"):
        sw.wait_until_ready(timeout=0.1)


# ---------------------------------------------------------------------------
# stop / kill / idempotency
# ---------------------------------------------------------------------------


def test_stop_before_start_is_noop(tmp_path: Path) -> None:
    sw = _make_switch(tmp_path)
    sw.stop()  # must not raise


def test_stop_after_already_exited_is_noop(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = 0
    proc.returncode = 0
    _patch_popen(mocker, proc=proc)
    sw = _make_switch(tmp_path)
    sw.start()
    sw.stop()
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


def test_stop_terminates_running_process(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = None  # alive at stop() entry
    proc.returncode = 0
    _patch_popen(mocker, proc=proc)
    sw = _make_switch(tmp_path)
    sw.start()
    sw.stop()
    proc.terminate.assert_called_once()
    proc.wait.assert_called()
    proc.kill.assert_not_called()


def test_stop_escalates_to_sigkill_on_timeout(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = None
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="x", timeout=5),  # first wait after SIGTERM
        0,  # second wait after SIGKILL succeeds
    ]
    _patch_popen(mocker, proc=proc)
    sw = _make_switch(tmp_path)
    sw.start()
    sw.stop(timeout=1.0)
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


def test_stop_is_idempotent(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    # Process becomes dead after the first stop.
    polls = iter([None, 0, 0, 0])
    proc.poll.side_effect = lambda: next(polls)
    proc.returncode = 0
    _patch_popen(mocker, proc=proc)
    sw = _make_switch(tmp_path)
    sw.start()
    sw.stop()
    sw.stop()
    sw.stop()
    proc.terminate.assert_called_once()


def test_kill_is_idempotent(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    polls = iter([None, 0, 0])
    proc.poll.side_effect = lambda: next(polls)
    proc.returncode = -9
    _patch_popen(mocker, proc=proc)
    sw = _make_switch(tmp_path)
    sw.start()
    sw.kill()
    sw.kill()
    proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_context_manager_starts_waits_and_stops(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = None
    _patch_popen(mocker, proc=proc)
    fake_sock = MagicMock()
    fake_sock.__enter__.return_value = fake_sock
    fake_sock.__exit__.return_value = None
    mocker.patch(
        "p4net.runtime.bmv2.socket.create_connection",
        return_value=fake_sock,
    )
    with _make_switch(tmp_path) as sw:
        assert isinstance(sw, BMv2Switch)
        assert sw.pid == 1
    proc.terminate.assert_called_once()


def test_context_manager_stops_on_body_exception(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = None
    _patch_popen(mocker, proc=proc)
    fake_sock = MagicMock()
    fake_sock.__enter__.return_value = fake_sock
    fake_sock.__exit__.return_value = None
    mocker.patch(
        "p4net.runtime.bmv2.socket.create_connection",
        return_value=fake_sock,
    )
    with pytest.raises(ValueError, match="from body"), _make_switch(tmp_path):
        raise ValueError("from body")
    proc.terminate.assert_called_once()


def test_context_manager_stops_on_ready_failure(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = 5  # exits before ready
    proc.returncode = 5
    _patch_popen(mocker, proc=proc)
    sw = _make_switch(tmp_path, startup_timeout=0.5)
    with pytest.raises(BMv2StartupError):
        sw.__enter__()
    # Even after a failed enter, the wrapper should have attempted cleanup.
    # Process was already exited so terminate is not called, but stop() ran.
    proc.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# boot_timestamp_us
# ---------------------------------------------------------------------------


def test_boot_timestamp_us_is_none_before_start(tmp_path: Path) -> None:
    sw = _make_switch(tmp_path)
    assert sw.boot_timestamp_us is None


def test_boot_timestamp_us_set_after_start(tmp_path: Path, mocker: MockerFixture) -> None:
    import time

    _patch_which(mocker)
    _patch_popen(mocker)
    sw = _make_switch(tmp_path)
    before = time.time_ns() // 1000
    sw.start()
    after = time.time_ns() // 1000
    assert sw.boot_timestamp_us is not None
    # Captured immediately before Popen — should fall in [before, after].
    assert before <= sw.boot_timestamp_us <= after


def test_boot_timestamp_us_sanity_wall_clock(tmp_path: Path, mocker: MockerFixture) -> None:
    """Result is in the same wall-clock epoch (not, e.g., a monotonic value)."""
    import time

    _patch_which(mocker)
    _patch_popen(mocker)
    sw = _make_switch(tmp_path)
    sw.start()
    now_us = time.time_ns() // 1000
    assert sw.boot_timestamp_us is not None
    assert abs(now_us - sw.boot_timestamp_us) < 5_000_000  # within 5 seconds


def test_boot_timestamp_us_cleared_on_stop(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = 0  # already exited fast path
    proc.returncode = 0
    _patch_popen(mocker, proc=proc)
    sw = _make_switch(tmp_path)
    sw.start()
    assert sw.boot_timestamp_us is not None
    sw.stop()
    assert sw.boot_timestamp_us is None


def test_boot_timestamp_us_cleared_on_popen_failure(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    _patch_which(mocker)
    mocker.patch(
        "p4net.runtime.bmv2.subprocess.Popen",
        side_effect=OSError("ENOEXEC"),
    )
    sw = _make_switch(tmp_path)
    with pytest.raises(BMv2StartupError):
        sw.start()
    # Even though we set it just before Popen, the OSError path must clear it.
    assert sw.boot_timestamp_us is None
