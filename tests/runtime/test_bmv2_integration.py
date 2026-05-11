"""Integration tests for `BMv2Switch` against a real `simple_switch_grpc`.

Gated by the `requires_bmv2` marker (see `tests/conftest.py`); skipped by
default. Run with `pytest --run-bmv2 -m requires_bmv2`. None of these tests
need root: BMv2 binds to 127.0.0.1 in the root namespace, no veths, no netns.

A pre-compiled bmv2 JSON is materialised once per test via the phase-3
`P4Compiler` against the small `forward.p4` fixture, so these tests also
implicitly need `p4c` on PATH; that's the same toolchain story as
`requires_p4c`, just applied to a different module.
"""

from __future__ import annotations

import contextlib
import shutil
import socket
import time
from pathlib import Path

import pytest

from p4net.compiler import P4Compiler
from p4net.runtime import BMv2StartupError, BMv2Switch

pytestmark = pytest.mark.requires_bmv2

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "p4"
_FORWARD = _FIXTURES / "forward.p4"


@pytest.fixture(scope="module")
def compiled_json(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile forward.p4 once for the whole module."""
    if shutil.which("p4c") is None:
        pytest.skip("p4c is required to materialise the BMv2 JSON for these tests")
    cache = tmp_path_factory.mktemp("compiler-cache")
    compiler = P4Compiler(cache_dir=cache)
    result = compiler.compile(_FORWARD)
    return result.bmv2_json


def _free_port() -> int:
    """Return a TCP port that was free at the moment of the call."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# 1. Empty-start lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.requires_p4c
def test_empty_start_lifecycle(compiled_json: Path, tmp_path: Path) -> None:
    sw = BMv2Switch(
        "s1",
        device_id=0,
        grpc_port=_free_port(),
        thrift_port=_free_port(),
        bmv2_json=compiled_json,
        port_to_iface={},
        log_dir=tmp_path / "logs",
        startup_timeout=10.0,
    )
    sw.start()
    try:
        sw.wait_until_ready()
        assert sw.is_running()
        # gRPC port should accept TCP connections.
        with socket.create_connection(("127.0.0.1", sw._grpc_port), timeout=2.0):
            pass
    finally:
        sw.stop()
    assert not sw.is_running()
    assert sw.returncode() is not None
    assert sw.log_file.is_file()
    assert sw.log_file.stat().st_size > 0


# ---------------------------------------------------------------------------
# 2. Context manager
# ---------------------------------------------------------------------------


@pytest.mark.requires_p4c
def test_context_manager_real(compiled_json: Path, tmp_path: Path) -> None:
    grpc_port = _free_port()
    thrift_port = _free_port()
    log_dir = tmp_path / "logs"
    name = "s_ctx"
    with BMv2Switch(
        name,
        device_id=1,
        grpc_port=grpc_port,
        thrift_port=thrift_port,
        bmv2_json=compiled_json,
        port_to_iface={},
        log_dir=log_dir,
    ) as sw:
        assert sw.is_running()
        with socket.create_connection(("127.0.0.1", grpc_port), timeout=2.0):
            pass
    assert not sw.is_running()
    log_path = log_dir / f"{name}.log"
    assert log_path.is_file()
    assert log_path.stat().st_size > 0


# ---------------------------------------------------------------------------
# 3. Bad JSON path
# ---------------------------------------------------------------------------


def test_bad_json_path_raises_startup_error(tmp_path: Path) -> None:
    bad_json = tmp_path / "nonexistent.json"
    log_dir = tmp_path / "logs"
    sw = BMv2Switch(
        "s_badjson",
        device_id=2,
        grpc_port=_free_port(),
        thrift_port=_free_port(),
        bmv2_json=bad_json,
        port_to_iface={},
        log_dir=log_dir,
        startup_timeout=8.0,
    )
    sw.start()  # spawn succeeds; BMv2 will exit shortly after
    try:
        with pytest.raises(BMv2StartupError) as info:
            sw.wait_until_ready()
        msg = str(info.value)
        assert "returncode" in msg
        log_path = log_dir / "s_badjson.log"
        assert log_path.is_file()
        # Some non-empty output indicating BMv2's complaint.
        assert log_path.stat().st_size > 0
    finally:
        sw.stop()


# ---------------------------------------------------------------------------
# 4. gRPC port collision
# ---------------------------------------------------------------------------


@pytest.mark.requires_p4c
def test_grpc_port_collision_raises(compiled_json: Path, tmp_path: Path) -> None:
    """When BMv2 cannot bind its gRPC port, the process dies and `is_running`
    eventually becomes False.

    Note: the held listener also accepts TCP probes on the same port, so the
    readiness check can briefly see a 'ready' port before BMv2 itself crashes.
    We therefore tolerate either outcome from `wait_until_ready` and assert
    only on the final process state.
    """
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        held_port = int(holder.getsockname()[1])
        sw = BMv2Switch(
            "s_collide",
            device_id=3,
            grpc_port=held_port,
            thrift_port=_free_port(),
            bmv2_json=compiled_json,
            port_to_iface={},
            log_dir=tmp_path / "logs",
            startup_timeout=6.0,
        )
        sw.start()
        try:
            with contextlib.suppress(BMv2StartupError):
                sw.wait_until_ready()
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and sw.is_running():
                time.sleep(0.1)
            assert not sw.is_running(), (
                f"BMv2 should have exited due to port collision; pid={sw.pid}"
            )
            rc = sw.returncode()
            assert rc is not None and rc != 0, f"unexpected returncode={rc}"
        finally:
            sw.stop()
    finally:
        holder.close()


# ---------------------------------------------------------------------------
# 5. stop() idempotency on a real process
# ---------------------------------------------------------------------------


@pytest.mark.requires_p4c
def test_stop_idempotent_on_real_process(compiled_json: Path, tmp_path: Path) -> None:
    sw = BMv2Switch(
        "s_idem",
        device_id=4,
        grpc_port=_free_port(),
        thrift_port=_free_port(),
        bmv2_json=compiled_json,
        port_to_iface={},
        log_dir=tmp_path / "logs",
    )
    sw.start()
    try:
        sw.wait_until_ready()
    finally:
        sw.stop()
    sw.stop()
    sw.stop()  # third call must not raise
    assert not sw.is_running()


# ---------------------------------------------------------------------------
# 6. boot_timestamp_us is a sane wall-clock value on a real process
# ---------------------------------------------------------------------------


@pytest.mark.requires_p4c
def test_boot_timestamp_us_real_wall_clock(compiled_json: Path, tmp_path: Path) -> None:
    import time

    sw = BMv2Switch(
        "s_boot",
        device_id=5,
        grpc_port=_free_port(),
        thrift_port=_free_port(),
        bmv2_json=compiled_json,
        port_to_iface={},
        log_dir=tmp_path / "logs",
    )
    assert sw.boot_timestamp_us is None
    before = time.time_ns() // 1000
    sw.start()
    after = time.time_ns() // 1000
    try:
        sw.wait_until_ready()
        assert sw.boot_timestamp_us is not None
        assert before <= sw.boot_timestamp_us <= after
        # Sanity: 2026 is comfortably past the Unix epoch and before any
        # plausible test-rig clock skew nightmare.
        assert sw.boot_timestamp_us > 1_700_000_000_000_000  # year 2023+
    finally:
        sw.stop()
    assert sw.boot_timestamp_us is None
