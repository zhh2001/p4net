"""Unit tests for `p4net.runtime.bmv2` with mocked child processes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pyroute2.netlink.exceptions import NetlinkError
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


def _start_diagnostic_switch(tmp_path: Path, mocker: MockerFixture) -> BMv2Switch:
    _patch_which(mocker)
    process = _patch_popen(mocker).return_value
    process.args = ["/usr/local/bin/simple_switch_grpc", str(tmp_path / "program.json")]
    switch = _make_switch(tmp_path)
    switch.start()
    mocker.patch("p4net.runtime.bmv2.time.monotonic", side_effect=[10.0, 10.0, 10.3, 10.35])
    return switch


def _mock_procfs(
    mocker: MockerFixture,
    *,
    read_errors: dict[str, OSError] | None = None,
    task_error: OSError | None = None,
) -> None:
    proc = Path("/proc/12345")
    original_read = Path.read_text
    original_iterdir = Path.iterdir

    def read(path: Path, *args: object, **kwargs: object) -> str:
        if not path.is_relative_to("/proc"):
            return original_read(path, *args, **kwargs)
        relative = str(path.relative_to(proc))
        if read_errors and relative in read_errors:
            raise read_errors[relative]
        return f"procfs {relative}\n"

    def tasks(path: Path) -> object:
        if not path.is_relative_to("/proc"):
            return original_iterdir(path)
        assert path == proc / "task"
        if task_error is not None:
            raise task_error
        return iter([proc / "task" / "12346", proc / "task" / "12345"])

    mocker.patch.object(Path, "read_text", autospec=True, side_effect=read)
    mocker.patch.object(Path, "iterdir", autospec=True, side_effect=tasks)


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


@pytest.mark.parametrize(
    "bad_level",
    [
        "",
        "FATAL",
        "info ",
        "verbose",
        [],
        ["info"],
        {},
        {"level": "info"},
        bytearray(b"info"),
        17,
        True,
        None,
        b"info",
    ],
)
def test_constructor_rejects_bad_log_level(tmp_path: Path, bad_level: object) -> None:
    with pytest.raises(ValueError, match="invalid log_level"):
        _make_switch(tmp_path, log_level=bad_level)  # type: ignore[arg-type]


@pytest.mark.parametrize("level", ["trace", "debug", "info", "warn", "error"])
def test_log_level_kept_in_process_arguments(tmp_path: Path, level: str) -> None:
    switch = _make_switch(tmp_path, log_level=level)
    argv = switch._build_argv()
    assert argv[argv.index("-L") + 1] == level
    assert switch.pid is None
    assert not switch.log_file.parent.exists()


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


@pytest.mark.parametrize(
    "bad_timeout",
    [
        0,
        -0.5,
        -10.0,
        float("nan"),
        float("inf"),
        -float("inf"),
        True,
        False,
        None,
        "1",
        b"1",
        [],
        {},
        10**400,
    ],
)
def test_constructor_rejects_invalid_startup_timeout(tmp_path: Path, bad_timeout: object) -> None:
    with pytest.raises(ValueError, match=r"startup_timeout.*positive"):
        _make_switch(tmp_path, startup_timeout=bad_timeout)  # type: ignore[arg-type]
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("timeout", [1, 1.5, 10])
def test_constructor_accepts_finite_positive_startup_timeout(
    tmp_path: Path, timeout: float
) -> None:
    switch = _make_switch(tmp_path, startup_timeout=timeout)
    assert switch.pid is None
    assert not switch.log_file.parent.exists()


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


def test_wait_until_ready_succeeds_with_owned_listeners(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = None
    _patch_popen(mocker, proc=proc)
    mocker.patch.object(BMv2Switch, "_listening_inodes", return_value=({"grpc"}, {"thrift"}))
    mocker.patch.object(BMv2Switch, "_socket_inodes", return_value={"grpc", "thrift"})
    diagnostics = mocker.patch.object(BMv2Switch, "_save_startup_diagnostics")
    sw = _make_switch(tmp_path)
    sw.start()
    sw.wait_until_ready(timeout=1.0)
    diagnostics.assert_not_called()


def test_wait_until_ready_raises_when_process_exits(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    proc = MagicMock()
    proc.pid = 1
    proc.poll.return_value = 7  # already exited with rc 7
    proc.returncode = 7
    _patch_popen(mocker, proc=proc)
    diagnostics = mocker.patch.object(BMv2Switch, "_save_startup_diagnostics")
    sw = _make_switch(tmp_path)
    sw.start()
    with pytest.raises(BMv2StartupError, match="returncode=7"):
        sw.wait_until_ready(timeout=1.0)
    diagnostics.assert_not_called()


def test_wait_until_ready_raises_on_timeout(tmp_path: Path, mocker: MockerFixture) -> None:
    _patch_which(mocker)
    _patch_popen(mocker)
    mocker.patch.object(BMv2Switch, "_save_startup_diagnostics", return_value=None)
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


@pytest.mark.parametrize("service", ["grpc", "thrift"])
def test_timeout_diagnostics_preserve_last_partial_observation(
    tmp_path: Path, mocker: MockerFixture, service: str
) -> None:
    switch = _start_diagnostic_switch(tmp_path, mocker)
    _mock_procfs(mocker)
    mocker.patch(
        "p4net.runtime.bmv2.time.monotonic",
        side_effect=[10.0, 10.0, 10.0, 10.05, 10.1, 10.2, 10.3, 10.35],
    )
    mocker.patch("p4net.runtime.bmv2.time.sleep")
    partial = ({"socket:[3]"}, set()) if service == "grpc" else (set(), {"socket:[3]"})
    listeners = mocker.patch.object(
        switch, "_listening_inodes", side_effect=[(set(), set()), partial]
    )
    mocker.patch.object(switch, "_socket_inodes", side_effect=[set(), {"socket:[4]", "socket:[3]"}])
    with pytest.raises(BMv2StartupError, match=r"0\.25s") as caught:
        switch.wait_until_ready(timeout=0.25)

    (diagnostic,) = (tmp_path / "logs").glob("*.startup-*.json")
    record = json.loads(diagnostic.read_text())
    assert str(diagnostic) in str(caught.value)
    assert record["pid"] == 12345
    assert record["argv"] == ["/usr/local/bin/simple_switch_grpc", str(tmp_path / "program.json")]
    assert record["returncode"] is None
    assert record["timeout_seconds"] == 0.25
    assert record["elapsed_seconds"] == pytest.approx(0.35)
    assert record["completed_polls"] == 2
    observation = record["last_observation"]
    assert observation["elapsed_seconds"] == pytest.approx(0.2)
    assert observation["grpc_socket_inodes"] == sorted(partial[0])
    assert observation["thrift_socket_inodes"] == sorted(partial[1])
    assert observation["owned_socket_inodes"] == ["socket:[3]", "socket:[4]"]
    assert record["process"]["status"] == "procfs status\n"
    assert record["threads"]["12346"]["wchan"] == "procfs task/12346/wchan\n"
    assert record["read_errors"] == {}
    assert listeners.call_count == 2


def test_timeout_diagnostics_distinguish_unobserved_listeners(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    switch = _start_diagnostic_switch(tmp_path, mocker)
    _mock_procfs(mocker)
    listeners = mocker.patch.object(switch, "_listening_inodes")
    descriptors = mocker.patch.object(switch, "_socket_inodes")
    with pytest.raises(BMv2StartupError, match="startup diagnostics:"):
        switch.wait_until_ready(timeout=0.25)

    (diagnostic,) = (tmp_path / "logs").glob("*.startup-*.json")
    record = json.loads(diagnostic.read_text())
    assert record["completed_polls"] == 0
    assert record["last_observation"] is None
    listeners.assert_not_called()
    descriptors.assert_not_called()


@pytest.mark.parametrize("exit_path", ["descriptor-scan", "ready-check"])
def test_timeout_diagnostics_skip_procfs_after_reaping_child(
    tmp_path: Path, mocker: MockerFixture, exit_path: str
) -> None:
    switch = _start_diagnostic_switch(tmp_path, mocker)
    process = switch._proc
    assert process is not None
    returncodes = iter([None, 7, 7])

    def poll() -> int | None:
        process.returncode = next(returncodes)
        return process.returncode

    mocker.patch.object(process, "poll", side_effect=poll)
    mocker.patch("p4net.runtime.bmv2.time.sleep")
    if exit_path == "descriptor-scan":
        times = [10.0, 10.0, 10.0, 10.3, 10.35]
        mocker.patch.object(switch, "_listening_inodes", return_value=(set(), set()))
        mocker.patch.object(switch, "_socket_inodes", side_effect=FileNotFoundError())
    else:
        times = [10.0, 10.0, 10.0, 10.1, 10.3, 10.35]
        mocker.patch.object(switch, "_listening_inodes", return_value=({"grpc"}, {"thrift"}))
        mocker.patch.object(switch, "_socket_inodes", return_value={"grpc", "thrift"})
    mocker.patch("p4net.runtime.bmv2.time.monotonic", side_effect=times)
    read = mocker.patch.object(
        Path, "read_text", side_effect=AssertionError("reaped PID must not be inspected")
    )
    tasks = mocker.patch.object(
        Path, "iterdir", side_effect=AssertionError("reaped PID must not be inspected")
    )

    with pytest.raises(BMv2StartupError, match="startup diagnostics:"):
        switch.wait_until_ready(timeout=0.25)

    read.assert_not_called()
    tasks.assert_not_called()
    (diagnostic,) = (tmp_path / "logs").glob("*.startup-*.json")
    with diagnostic.open(encoding="utf-8") as source:
        record = json.load(source)
    assert record["pid"] == 12345
    assert record["returncode"] == 7
    assert record["process"] is None
    assert record["threads"] == {}
    if exit_path == "descriptor-scan":
        assert record["completed_polls"] == 0
        assert record["last_observation"] is None
    else:
        assert record["completed_polls"] == 1
        assert record["last_observation"]["grpc_socket_inodes"] == ["grpc"]
        assert record["last_observation"]["thrift_socket_inodes"] == ["thrift"]


@pytest.mark.parametrize("failure", ["read", "task-list"])
def test_timeout_diagnostics_preserve_partial_procfs_on_errors(
    tmp_path: Path, mocker: MockerFixture, failure: str
) -> None:
    switch = _start_diagnostic_switch(tmp_path, mocker)
    if failure == "read":
        _mock_procfs(
            mocker,
            read_errors={
                "stack": PermissionError("stack access denied"),
                "task/12346/status": FileNotFoundError("thread exited"),
            },
        )
    else:
        _mock_procfs(mocker, task_error=FileNotFoundError("process exited"))
    with pytest.raises(BMv2StartupError, match="startup diagnostics:"):
        switch.wait_until_ready(timeout=0.25)

    (diagnostic,) = (tmp_path / "logs").glob("*.startup-*.json")
    record = json.loads(diagnostic.read_text())
    assert record["process"]["status"] == "procfs status\n"
    assert record["process"]["stat"] == "procfs stat\n"
    if failure == "read":
        assert record["process"]["stack"] is None
        assert record["threads"]["12346"]["status"] is None
        assert record["threads"]["12346"]["wchan"] == "procfs task/12346/wchan\n"
        assert record["read_errors"] == {
            "stack": "stack access denied",
            "task/12346/status": "thread exited",
        }
    else:
        assert record["threads"] == {}
        assert record["read_errors"] == {"task": "process exited"}


@pytest.mark.parametrize("failure", ["capture", "serialization", "open", "write"])
def test_diagnostic_failure_preserves_timeout_without_artifact_link(
    tmp_path: Path, mocker: MockerFixture, failure: str
) -> None:
    switch = _start_diagnostic_switch(tmp_path, mocker)
    _mock_procfs(mocker)
    if failure == "capture":
        mocker.patch.object(Path, "read_text", side_effect=RuntimeError("snapshot failed"))
    elif failure == "serialization":
        mocker.patch("p4net.runtime.bmv2.json.dumps", side_effect=TypeError("invalid value"))
    elif failure == "open":
        mocker.patch.object(Path, "open", side_effect=PermissionError("read-only directory"))
    else:
        output = mocker.patch.object(Path, "open").return_value.__enter__.return_value
        output.write.side_effect = OSError("disk full")

    with pytest.raises(BMv2StartupError) as caught:
        switch.wait_until_ready(timeout=0.25)

    assert str(caught.value) == (
        f"BMv2 's1' did not open its gRPC and Thrift ports within 0.25s; see {switch.log_file}"
    )
    assert not list((tmp_path / "logs").glob("*.startup-*.json"))


def test_wait_until_ready_requires_start_first(tmp_path: Path) -> None:
    sw = _make_switch(tmp_path)
    with pytest.raises(BMv2StartupError, match="has not been started"):
        sw.wait_until_ready(timeout=0.1)


@pytest.mark.parametrize(
    "timeout",
    [
        0,
        -0.5,
        -10.0,
        float("nan"),
        float("inf"),
        -float("inf"),
        True,
        False,
        "1",
        b"1",
        [],
        {},
        10**400,
    ],
)
def test_wait_until_ready_rejects_invalid_timeout_before_inspecting_listeners(
    tmp_path: Path, mocker: MockerFixture, timeout: object
) -> None:
    _patch_which(mocker)
    process = _patch_popen(mocker).return_value
    switch = _make_switch(tmp_path)
    switch.start()
    clock = mocker.patch("p4net.runtime.bmv2.time.monotonic", return_value=0.0)
    resolve = mocker.patch("p4net.runtime.bmv2.socket.getaddrinfo")
    listeners = mocker.patch.object(switch, "_listening_inodes")
    with pytest.raises(ValueError, match=r"timeout.*positive"):
        switch.wait_until_ready(timeout=timeout)  # type: ignore[arg-type]
    clock.assert_not_called()
    resolve.assert_not_called()
    listeners.assert_not_called()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


@pytest.mark.parametrize("timeout", [1, 1.5, None])
def test_wait_until_ready_accepts_finite_positive_or_default_timeout(
    tmp_path: Path, mocker: MockerFixture, timeout: float | None
) -> None:
    _patch_which(mocker)
    _patch_popen(mocker)
    switch = _make_switch(tmp_path)
    switch.start()
    mocker.patch.object(switch, "_listening_inodes", return_value=({"grpc"}, {"thrift"}))
    mocker.patch.object(switch, "_socket_inodes", return_value={"grpc", "thrift"})
    switch.wait_until_ready(timeout=timeout)


@pytest.mark.parametrize("explicit_none", [False, True])
def test_wait_until_ready_uses_configured_startup_timeout(
    tmp_path: Path, mocker: MockerFixture, explicit_none: bool
) -> None:
    _patch_which(mocker)
    _patch_popen(mocker)
    switch = _make_switch(tmp_path, startup_timeout=0.25)
    switch.start()
    mocker.patch(
        "p4net.runtime.bmv2.time.monotonic", side_effect=[10.0, 10.0, 10.0, 10.05, 10.3, 10.3]
    )
    mocker.patch.object(switch, "_save_startup_diagnostics", return_value=None)
    sleep = mocker.patch("p4net.runtime.bmv2.time.sleep")
    listeners = mocker.patch.object(switch, "_listening_inodes", return_value=(set(), set()))
    mocker.patch.object(switch, "_socket_inodes", return_value=set())
    with pytest.raises(BMv2StartupError, match=r"0\.25s"):
        if explicit_none:
            switch.wait_until_ready(timeout=None)
        else:
            switch.wait_until_ready()
    listeners.assert_called_once()
    sleep.assert_called_once_with(0.1)


@pytest.mark.parametrize("error", [PermissionError("access denied"), NetlinkError(1)])
def test_wait_until_ready_reports_listener_inspection_failure(
    tmp_path: Path, mocker: MockerFixture, error: Exception
) -> None:
    _patch_which(mocker)
    _patch_popen(mocker)
    mocker.patch.object(BMv2Switch, "_listening_inodes", side_effect=error)
    sw = _make_switch(tmp_path)
    sw.start()
    with pytest.raises(BMv2StartupError, match="cannot inspect") as caught:
        sw.wait_until_ready(timeout=1.0)
    assert caught.value.__cause__ is error


def test_wait_until_ready_reports_address_resolution_failure(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    _patch_which(mocker)
    _patch_popen(mocker)
    error = OSError("name resolution failed")
    mocker.patch("p4net.runtime.bmv2.socket.getaddrinfo", side_effect=error)
    sw = _make_switch(tmp_path)
    sw.start()
    with pytest.raises(BMv2StartupError, match="cannot resolve") as caught:
        sw.wait_until_ready(timeout=1.0)
    assert caught.value.__cause__ is error


def test_wait_until_ready_reports_exit_during_descriptor_scan(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    _patch_which(mocker)
    proc = _patch_popen(mocker).return_value
    proc.poll.side_effect = [None, 7, 7, 7]
    mocker.patch.object(BMv2Switch, "_listening_inodes", return_value=(set(), set()))
    mocker.patch.object(BMv2Switch, "_socket_inodes", side_effect=FileNotFoundError())
    sw = _make_switch(tmp_path)
    sw.start()
    with pytest.raises(BMv2StartupError, match="returncode=7"):
        sw.wait_until_ready(timeout=1.0)


# ---------------------------------------------------------------------------
# stop / kill / idempotency
# ---------------------------------------------------------------------------


def test_stop_before_start_is_noop(tmp_path: Path) -> None:
    sw = _make_switch(tmp_path)
    sw.stop()  # must not raise


@pytest.mark.parametrize("state", ["unstarted", "running", "exited"])
@pytest.mark.parametrize(
    "timeout",
    [
        0,
        -0.5,
        -10.0,
        float("nan"),
        float("inf"),
        -float("inf"),
        True,
        False,
        None,
        "1",
        b"1",
        [],
        {},
        10**400,
    ],
)
def test_stop_rejects_invalid_timeout_without_side_effects(
    tmp_path: Path, mocker: MockerFixture, state: str, timeout: object
) -> None:
    _patch_which(mocker)
    process = _patch_popen(mocker).return_value
    switch = _make_switch(tmp_path)
    if state != "unstarted":
        switch.start()
    if state == "exited":
        process.poll.return_value = 0
    timestamp = switch.boot_timestamp_us
    handle = switch._log_file_handle
    try:
        with pytest.raises(ValueError, match=r"timeout.*positive"):
            switch.stop(timeout=timeout)  # type: ignore[arg-type]
        process.poll.assert_not_called()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        process.wait.assert_not_called()
        assert switch.boot_timestamp_us == timestamp
        assert switch._log_file_handle is handle
        if handle is not None:
            assert not handle.closed
        else:
            assert not switch.log_file.parent.exists()
    finally:
        switch.stop()


@pytest.mark.parametrize("state", ["unstarted", "running", "exited"])
@pytest.mark.parametrize("timeout", [1, 1.5])
def test_stop_accepts_finite_positive_timeout(
    tmp_path: Path, mocker: MockerFixture, state: str, timeout: float
) -> None:
    _patch_which(mocker)
    process = _patch_popen(mocker).return_value
    switch = _make_switch(tmp_path)
    if state != "unstarted":
        switch.start()
    if state == "exited":
        process.poll.return_value = 0
    handle = switch._log_file_handle
    switch.stop(timeout=timeout)
    if state == "running":
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=timeout)
    else:
        process.terminate.assert_not_called()
        process.wait.assert_not_called()
    process.kill.assert_not_called()
    assert switch.boot_timestamp_us is None
    assert switch._log_file_handle is None
    if handle is not None:
        assert handle.closed


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


@pytest.mark.parametrize("timeout", [1, 1.5])
def test_stop_escalates_to_sigkill_on_timeout(
    tmp_path: Path, mocker: MockerFixture, timeout: float
) -> None:
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
    sw.stop(timeout=timeout)
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.wait.call_args_list == [mocker.call(timeout=timeout), mocker.call(timeout=timeout)]


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
    mocker.patch.object(BMv2Switch, "_listening_inodes", return_value=({"grpc"}, {"thrift"}))
    mocker.patch.object(BMv2Switch, "_socket_inodes", return_value={"grpc", "thrift"})
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
    mocker.patch.object(BMv2Switch, "_listening_inodes", return_value=({"grpc"}, {"thrift"}))
    mocker.patch.object(BMv2Switch, "_socket_inodes", return_value={"grpc", "thrift"})
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


def test_boot_timestamp_us_cleared_on_popen_failure(tmp_path: Path, mocker: MockerFixture) -> None:
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
