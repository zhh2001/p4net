"""Resource accounting and functional checks for the standalone benchmark."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import benchmark


def test_stat_handles_parentheses_and_ignores_child_cpu() -> None:
    # Fields 3..22: state, ppid, ..., utime, stime, cutime, cstime, ..., starttime.
    fields = ["S"] + ["0"] * 19
    fields[11:15] = ["120", "30", "900", "800"]
    fields[19] = "12345"
    assert benchmark.process_stat("42 (worker ) name) " + " ".join(fields)) == (12345, 150)
    fields[0] = "Z"
    with pytest.raises(RuntimeError, match="exited"):
        benchmark.process_stat("42 (worker) " + " ".join(fields))


def test_idle_accounting_sums_processes_and_allows_multiple_cpu_cores() -> None:
    samples = [
        {
            "monotonic_ns": 1_000_000_000,
            "processes": [
                {"pid": 1, "start_ticks": 5, "cpu_ticks": 100, "pss_kib": 1024, "rss_kib": 3072},
                {"pid": 2, "start_ticks": 6, "cpu_ticks": 200, "pss_kib": 2048, "rss_kib": 4096},
            ],
        },
        {
            "monotonic_ns": 3_000_000_000,
            "processes": [
                {"pid": 1, "start_ticks": 5, "cpu_ticks": 300, "pss_kib": 3072, "rss_kib": 4096},
                {"pid": 2, "start_ticks": 6, "cpu_ticks": 400, "pss_kib": 2048, "rss_kib": 5120},
            ],
        },
    ]
    assert benchmark.idle_summary(samples, 100) == {
        "elapsed_s": 2.0,
        "cpu_seconds": 4.0,
        "cpu_percent": 200.0,
        "mean_pss_mib": 4.0,
        "mean_rss_mib": 8.0,
    }
    samples[1]["processes"][1]["start_ticks"] = 7
    with pytest.raises(RuntimeError, match="group changed"):
        benchmark.idle_summary(samples, 100)


def test_rollup_uses_pss_and_rss_not_private_or_swap() -> None:
    text = "1234-5678 ---p 0000 00:00 0 [rollup]\nRss: 4096 kB\nPss: 2048 kB\nSwap: 8192 kB\n"
    assert benchmark.memory_kib(text) == (2048, 4096)


def test_candidate_ports_are_distinct_and_reserved() -> None:
    import socket

    with benchmark.reserve_ports(16) as ports:
        assert len(set(ports)) == 16
        for port in ports:
            with socket.socket() as sock, pytest.raises(OSError):
                sock.bind(("127.0.0.1", port))


@pytest.mark.parametrize(
    "option,value",
    [
        ("--trials", "0"),
        ("--switches", "0"),
        ("--switches", "65"),
        ("--idle-seconds", "nan"),
        ("--idle-seconds", "0"),
        ("--interval", "inf"),
        ("--settle", "-1"),
    ],
)
def test_reject_invalid_measurement_settings(option: str, value: str, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        benchmark.parse_args(["--output", str(tmp_path), option, value])


def test_finish_worker_requests_cleanup_on_eof(tmp_path: Path) -> None:
    marker = tmp_path / "cleaned"
    code = (
        "import pathlib, sys; print('ready', flush=True); "
        "sys.stdin.readline(); pathlib.Path(sys.argv[1]).write_text('cleaned')"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(marker)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    benchmark.finish_worker(proc, timeout=10)
    assert proc.returncode == 0
    assert marker.read_text() == "cleaned"


def test_coordinator_cleans_worker_when_protocol_fails(tmp_path: Path) -> None:
    marker = tmp_path / "cleaned"
    code = (
        "import pathlib, sys; print('invalid-json', flush=True); "
        "sys.stdin.readline(); pathlib.Path(sys.argv[1]).write_text('cleaned')"
    )
    popen = subprocess.Popen
    args = benchmark.parse_args(["--check", "--output", str(tmp_path)])
    with (
        patch.object(
            benchmark.subprocess,
            "Popen",
            side_effect=lambda *a, **kw: popen(
                [sys.executable, "-c", code, str(marker)],
                **kw,
            ),
        ),
        pytest.raises(json.JSONDecodeError),
    ):
        benchmark.run_trial(args, 1, "empty", tmp_path / "trial")
    assert marker.read_text() == "cleaned"
    assert not (tmp_path / "trial/result.json").exists()


def test_measurement_pipeline_with_synthetic_samples(tmp_path: Path) -> None:
    code = (
        "import json, sys; print(json.dumps({'pids': [123]}), flush=True); "
        "sys.stdin.readline(); print(json.dumps({'start_s': 2, 'stop_s': 1}), flush=True)"
    )
    popen = subprocess.Popen
    args = benchmark.parse_args(
        [
            "--output",
            str(tmp_path),
            "--idle-seconds",
            "0.02",
            "--interval",
            "0.01",
            "--settle",
            "0.001",
        ]
    )
    counter = 0

    def synthetic_snapshot(pids: list[int]) -> dict:
        nonlocal counter
        counter += 1
        return {
            "monotonic_ns": counter * 1_000_000_000,
            "processes": [
                {
                    "pid": pids[0],
                    "start_ticks": 1,
                    "cpu_ticks": counter * 100,
                    "pss_kib": 1024,
                    "rss_kib": 2048,
                }
            ],
        }

    with (
        patch.object(
            benchmark.subprocess,
            "Popen",
            side_effect=lambda *a, **kw: popen(
                [sys.executable, "-c", code],
                **kw,
            ),
        ),
        patch.object(benchmark, "snapshot", side_effect=synthetic_snapshot),
        patch.object(
            benchmark.os,
            "sysconf",
            return_value=100,
        ),
    ):
        result = benchmark.run_trial(args, 1, "warm", tmp_path / "trial")
    assert result["idle"]["cpu_percent"] == 100
    assert result["idle"]["mean_pss_mib"] == 1
    assert result["idle"]["mean_rss_mib"] == 2
    assert len(result["samples"]) >= 2
    assert json.loads((tmp_path / "trial/result.json").read_text()) == result
    benchmark.write_summary(tmp_path / "summary.md", [result])
    assert (
        "| 1 | 2 | warm | 1 | 2.000 | 1.000 | 100.000 | 1.000 | 2.000 |"
        in (tmp_path / "summary.md").read_text()
    )


@pytest.mark.integration
@pytest.mark.requires_p4c
@pytest.mark.requires_bmv2
def test_check_runs_fresh_workers_and_cleans_native_resources(tmp_path: Path) -> None:
    output = tmp_path / "check"
    subprocess.run(
        [
            sys.executable,
            str(Path(benchmark.__file__)),
            "--check",
            "--switches",
            "1",
            "2",
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    results = json.loads((output / "results.json").read_text())
    assert len(results) == 4
    assert len({r["pids"][0] for r in results}) == 4
    for result in results:
        assert result["cleanup_ok"]
        assert len(result["connectivity"]) == result["switches"] * 2
        assert all(check["ok"] for check in result["connectivity"])
        assert not {"idle", "samples", "start_s", "stop_s"}.intersection(result)
        assert all(not Path(f"/proc/{pid}").exists() for pid in result["pids"])
    assert not (output / "summary.md").exists()


@pytest.mark.integration
@pytest.mark.requires_p4c
@pytest.mark.requires_bmv2
def test_failed_native_connectivity_cleans_topology_and_does_not_report_result(
    tmp_path: Path,
) -> None:
    source = tmp_path / "drop.p4"
    source.write_text(
        benchmark.P4_SOURCE.read_text()
        .replace("std.egress_spec = 2;", "mark_to_drop(std);")
        .replace("std.egress_spec = 1;", "mark_to_drop(std);")
    )
    output = tmp_path / "failed-check"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(benchmark.__file__)),
            "--check",
            "--switches",
            "1",
            "--p4-source",
            str(source),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode != 0
    trial = output / "n1-empty-1"
    assert "failed its connectivity check" in (trial / "worker.log").read_text()
    assert not any(
        check["ok"] for check in json.loads((trial / "failed-connectivity.json").read_text())
    )
    topology = json.loads((trial / "topology.json").read_text())
    assert all(not (Path("/var/run/netns") / name).exists() for name in topology["hosts"])
    assert all(
        not (Path("/sys/class/net") / f"{name}-eth{port}").exists()
        for name in topology["switches"]
        for port in (1, 2)
    )
    assert not (trial / "result.json").exists()
    assert not (output / "results.json").exists()
    assert not (output / "summary.md").exists()
