"""Measure topology lifecycle and idle resources in fresh Python workers."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import select
import socket
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
P4_SOURCE = PROJECT / "examples/quick_start/quick_start.p4"


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


@contextlib.contextmanager
def reserve_ports(count: int) -> Iterator[list[int]]:
    """Keep candidate ports distinct until the topology is ready to start."""
    with contextlib.ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(count)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        yield [int(sock.getsockname()[1]) for sock in sockets]


def process_stat(text: str) -> tuple[int, int]:
    """Read process identity and user + system CPU ticks from /proc/PID/stat."""
    fields = text.rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X", "x"}:
        raise RuntimeError("measured process has exited")
    return int(fields[19]), int(fields[11]) + int(fields[12])


def memory_kib(text: str) -> tuple[int, int]:
    fields = {line.split(":", 1)[0]: line.split()[1] for line in text.splitlines() if ":" in line}
    return int(fields["Pss"]), int(fields["Rss"])


def snapshot(pids: Sequence[int]) -> dict[str, Any]:
    processes = []
    for pid in pids:
        proc = Path(f"/proc/{pid}")
        identity, ticks = process_stat((proc / "stat").read_text())
        pss, rss = memory_kib((proc / "smaps_rollup").read_text())
        if process_stat((proc / "stat").read_text())[0] != identity:
            raise RuntimeError(f"PID {pid} was reused during sampling")
        processes.append(
            {
                "pid": pid,
                "start_ticks": identity,
                "cpu_ticks": ticks,
                "pss_kib": pss,
                "rss_kib": rss,
            }
        )
    return {"monotonic_ns": time.perf_counter_ns(), "processes": processes}


def idle_summary(samples: list[dict[str, Any]], ticks_per_second: int) -> dict[str, float]:
    first, last = samples[0], samples[-1]
    identities = [{(p["pid"], p["start_ticks"]) for p in s["processes"]} for s in samples]
    if any(identity != identities[0] for identity in identities[1:]):
        raise RuntimeError("measured process group changed during the idle window")
    elapsed = (last["monotonic_ns"] - first["monotonic_ns"]) / 1e9
    cpu_seconds = (
        sum(p["cpu_ticks"] for p in last["processes"])
        - sum(p["cpu_ticks"] for p in first["processes"])
    ) / ticks_per_second
    if elapsed <= 0 or cpu_seconds < 0:
        raise RuntimeError("invalid CPU sampling interval")
    return {
        "elapsed_s": elapsed,
        "cpu_seconds": cpu_seconds,
        "cpu_percent": cpu_seconds / elapsed * 100,
        "mean_pss_mib": statistics.mean(
            sum(p["pss_kib"] for p in s["processes"]) / 1024 for s in samples
        ),
        "mean_rss_mib": statistics.mean(
            sum(p["rss_kib"] for p in s["processes"]) / 1024 for s in samples
        ),
    }


def worker(args: argparse.Namespace) -> None:
    # The coordinator must stay free of gRPC state across trials.
    from p4net import Network
    from p4net.compiler import P4Compiler
    from p4net.topo import Topology

    directory = args.output
    compiler = P4Compiler(cache_dir=directory / "compiler")
    if args.cache == "warm":
        compiler.compile(args.p4_source)
    topo = Topology()
    pairs = []
    with reserve_ports(2 * args.count) as ports:
        for i in range(args.count):
            token = uuid.uuid4().hex[:6]
            switch = topo.add_switch(
                f"w{token}",
                p4_src=args.p4_source,
                device_id=i,
                grpc_port=ports[2 * i],
                thrift_port=ports[2 * i + 1],
                pcap_enabled=False,
            )
            hosts = [
                topo.add_host(
                    f"b{token}{letter}",
                    ip=f"10.0.{i}.{j}/24",
                    mac=f"02:00:00:00:{i:02x}:{j:02x}",
                )
                for j, letter in ((1, "a"), (2, "b"))
            ]
            for port, host in enumerate(hosts, 1):
                topo.add_link(host, switch, port_b=port)
            pairs.append((hosts[0].name, hosts[1].name))
        write_json(
            directory / "topology.json",
            {"hosts": list(topo.hosts), "switches": list(topo.switches)},
        )
    net = Network(topo, compiler=compiler, log_dir=directory / "bmv2")
    result: dict[str, Any] = {}
    switches = []
    try:
        start = time.perf_counter_ns()
        net.start()
        if not args.check:
            result["start_s"] = (time.perf_counter_ns() - start) / 1e9
        switches = [sw.bmv2 for sw in net.switches.values()]
        hits = [sw.compile_result.cache_hit for sw in net.switches.values()]
        expected = (
            [True] * args.count if args.cache == "warm" else [False] + [True] * (args.count - 1)
        )
        if hits != expected:
            raise RuntimeError(f"unexpected compiler cache hits: {hits}, expected {expected}")
        result["cache_hits"] = hits
        checks = []
        for a, b in pairs:
            for src, dst in ((a, b), (b, a)):
                checks.append({"src": src, "dst": dst, "ok": net.ping(src, dst, count=1)})
        result["connectivity"] = checks
        if not all(check["ok"] for check in checks):
            write_json(directory / "failed-connectivity.json", checks)
            raise RuntimeError("a host pair failed its connectivity check")
        pids = [os.getpid()]
        for sw in net.switches.values():
            pid = sw.bmv2.pid
            if pid is None:
                raise RuntimeError("BMv2 process has no PID")
            pids.append(pid)
        print(json.dumps({"pids": pids}), flush=True)
        # EOF also requests cleanup when the coordinator exits early.
        sys.stdin.readline()
    finally:
        stop = time.perf_counter_ns()
        net.stop()
        if not args.check:
            result["stop_s"] = (time.perf_counter_ns() - stop) / 1e9
    leftovers = [name for name in topo.hosts if (Path("/var/run/netns") / name).exists()]
    leftovers += [
        f"{name}-eth{port}"
        for name in topo.switches
        for port in (1, 2)
        if (Path("/sys/class/net") / f"{name}-eth{port}").exists()
    ]
    if leftovers or any(sw.is_running() for sw in switches):
        raise RuntimeError(f"incomplete topology cleanup: {leftovers}")
    result["cleanup_ok"] = True
    print(json.dumps(result), flush=True)


def read_event(proc: subprocess.Popen[str], timeout: float) -> dict[str, Any]:
    assert proc.stdout is not None
    readable, _, _ = select.select([proc.stdout], [], [], timeout)
    if not readable:
        raise TimeoutError(f"worker did not respond within {timeout}s")
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("worker exited before returning a result; see worker.log")
    value: dict[str, Any] = json.loads(line)
    return value


def finish_worker(proc: subprocess.Popen[str], timeout: float) -> None:
    """Request ordinary teardown before escalating an unresponsive worker."""
    if proc.stdin is not None:
        with contextlib.suppress(BrokenPipeError):
            proc.stdin.close()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # The worker and its children share a separate process group.
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise RuntimeError(
                "worker required SIGKILL; inspect topology.json and worker.log"
            ) from None
    finally:
        if proc.stdout is not None:
            proc.stdout.close()


def run_trial(args: argparse.Namespace, count: int, cache: str, directory: Path) -> dict[str, Any]:
    directory.mkdir()
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--count",
        str(count),
        "--cache",
        cache,
        "--p4-source",
        str(args.p4_source),
        "--output",
        str(directory),
    ]
    if args.check:
        command.append("--check")
    with (directory / "worker.log").open("w") as log:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
            start_new_session=True,
        )
        try:
            ready = read_event(proc, timeout=300)
            pids = ready["pids"]
            samples = []
            if args.check:
                snapshot(pids)  # Check /proc permissions without collecting a baseline.
            else:
                time.sleep(args.settle)
                samples.append(snapshot(pids))
                deadline = time.perf_counter() + args.idle_seconds
                while (remaining := deadline - time.perf_counter()) > 0:
                    time.sleep(min(args.interval, remaining))
                    samples.append(snapshot(pids))
            assert proc.stdin is not None
            proc.stdin.write("stop\n")
            proc.stdin.flush()
            result = read_event(proc, timeout=30 + count * 15)
            if proc.wait(timeout=10) != 0:
                raise RuntimeError("worker failed; see worker.log")
        finally:
            finish_worker(proc, timeout=30 + count * 15)
    result.update({"switches": count, "hosts": 2 * count, "cache": cache, "pids": pids})
    if not args.check:
        result["idle"] = idle_summary(samples, os.sysconf("SC_CLK_TCK"))
        result["samples"] = samples
    write_json(directory / "result.json", result)
    return result


def command_output(argv: list[str]) -> str:
    result = subprocess.run(
        argv, capture_output=True, text=True, check=True, cwd=PROJECT, timeout=30
    )
    return (result.stdout + result.stderr).strip()


def environment(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "recorded_at": datetime.now().astimezone().isoformat(),
        "revision": command_output(["git", "rev-parse", "HEAD"]),
        "working_tree": command_output(["git", "status", "--short"]),
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "os_release": Path("/etc/os-release").read_text(),
        "cpuinfo": Path("/proc/cpuinfo").read_text(),
        "meminfo": Path("/proc/meminfo").read_text(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "load_average": os.getloadavg(),
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in (
                "p4net",
                "grpcio",
                "pyroute2",
                "protobuf",
                "p4runtime",
            )
        },
        "p4c": command_output(["p4c", "--version"]),
        "bmv2": command_output(["simple_switch_grpc", "--version"]),
        "iproute2": command_output(["ip", "-Version"]),
        "p4_source": str(args.p4_source),
        "p4_sha256": hashlib.sha256(args.p4_source.read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"),
        "settings": {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(args).items()
        },
        "bmv2_log_level": "info",
        "pcap": False,
    }


def write_summary(path: Path, results: list[dict[str, Any]]) -> None:
    with path.open("w") as stream:
        print(
            "| Switches | Hosts | Cache | Trials | Start (s) | Stop (s) | "
            "Idle CPU (%) | PSS (MiB) | RSS sum (MiB) |",
            file=stream,
        )
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |", file=stream)
        for count, cache in sorted({(r["switches"], r["cache"]) for r in results}):
            group = [r for r in results if (r["switches"], r["cache"]) == (count, cache)]
            values = [statistics.median(r[key] for r in group) for key in ("start_s", "stop_s")] + [
                statistics.median(r["idle"][key] for r in group)
                for key in (
                    "cpu_percent",
                    "mean_pss_mib",
                    "mean_rss_mib",
                )
            ]
            cells = " | ".join(f"{value:.3f}" for value in values)
            print(f"| {count} | {2 * count} | {cache} | {len(group)} | {cells} |", file=stream)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--switches", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--idle-seconds", type=float, default=5.0)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--settle", type=float, default=1.0)
    parser.add_argument("--p4-source", type=Path, default=P4_SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="check topology, cache and cleanup without recording performance",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--count", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument(
        "--cache", choices=("empty", "warm"), default="empty", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    if args.trials < 1 or any(not 1 <= n <= 64 for n in [*args.switches, args.count]):
        parser.error("trials must be positive and switch counts must be between 1 and 64")
    for name in ("idle_seconds", "interval", "settle"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{name.replace('_', '-')} must be finite and positive")
    args.output = args.output.resolve()
    args.p4_source = args.p4_source.resolve(strict=True)
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if os.geteuid() != 0:
        raise SystemExit("run as root to create network namespaces and read process memory")
    if args.worker:
        worker(args)
        return
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / "environment.json", environment(args))
    results = []
    for trial in range(1 if args.check else args.trials):
        sizes = list(dict.fromkeys(args.switches))
        if trial % 2:
            sizes.reverse()
        for count in sizes:
            # Alternate cache order to reduce correlation with run order.
            for cache in ("empty", "warm") if trial % 2 == 0 else ("warm", "empty"):
                name = f"n{count}-{cache}-{trial + 1}"
                print(name, flush=True)
                results.append(run_trial(args, count, cache, args.output / name))
                write_json(args.output / "results.json", results)
    if not args.check:
        write_summary(args.output / "summary.md", results)
    print(f"{'Checks' if args.check else 'Measurements'} complete: {args.output}")


if __name__ == "__main__":
    main()
