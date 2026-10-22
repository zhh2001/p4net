---
description: Reproduce topology startup, shutdown, idle CPU and memory measurements with recorded environments and raw samples.
---

# Performance baseline

The baseline measures how long a local topology takes to start and stop, how
much CPU and memory its running processes use while idle, and whether its
host pairs can communicate. Run it against the checkout you want to measure.

## Methodology

[`tools/benchmark.py`](https://github.com/zhh2001/p4net/blob/main/tools/benchmark.py)
builds 1, 2, 4 and 8 switches, each connected to two hosts. The switch/host
groups are independent: increasing the switch count increases the number of
processes and namespaces, not the number of forwarding hops. Each switch runs
the bundled `examples/quick_start/quick_start.p4` port-swap pipeline. BMv2 uses
its default `info` log level, with packet capture disabled and no link impairments.

Each topology size runs five trials in each of two compiler-cache states:

- **Empty**: a new artifact directory for the trial. The first switch compiles
  the program and the remaining switches reuse those artifacts.
- **Warm**: compile the program into a new trial directory before timing startup.
  All switches reuse the artifacts. P4 preprocessing still runs on each call.

These labels describe p4net's compiler artifact cache. The script does not clear
the operating system's file cache. Cache hits are checked against the expected
pattern and saved for each trial. Cache and topology-size order alternate between trials.

Every trial starts a fresh Python interpreter. The coordinator never opens a
gRPC connection, so one trial's gRPC threads cannot affect the next trial's
process creation. Garbage collection keeps its normal settings.

| Metric | Definition |
| --- | --- |
| Start (s) | Wall time immediately around `Network.start()`. Includes compilation/cache lookup, namespace and interface setup, sequential BMv2 startup, P4Runtime connection and pipeline installation. Excludes interpreter startup, topology construction and warm-cache preparation. |
| Stop (s) | Wall time immediately around `Network.stop()`, including control connection closure, BMv2 shutdown, interface and namespace removal. |
| Idle CPU (%) | Sum of user and system CPU time for the topology's Python worker and all its BMv2 processes, divided by the actual sampling interval. 100% means one logical CPU, so the sum can exceed 100%. |
| PSS (MiB) | Mean of sampled, summed proportional resident memory for those same processes. Shared mappings are divided among the processes that map them. |
| RSS sum (MiB) | Mean of sampled, summed resident memory for those same processes. Shared pages can be counted more than once. |
| Connectivity | One IPv4 ping in each direction within every host pair. Every check must pass for the trial to produce a result. |

Startup and shutdown use Python's
[`perf_counter_ns()`](https://docs.python.org/3/library/time.html#time.perf_counter_ns).
After the connectivity checks, the topology settles for one second. The
coordinator then samples the idle process group every 250 ms for five seconds.
It reads CPU ticks from `/proc/PID/stat` and memory from `/proc/PID/smaps_rollup`,
as described in the [Linux proc documentation](https://www.kernel.org/doc/html/latest/filesystems/proc.html).
Process identities are checked throughout the window.

The resource figures cover the Python worker and BMv2 processes during that idle
window. They exclude the coordinator, compiler subprocesses, kernel networking
memory and other applications. Memory is an average of snapshots, not a peak
for the full start/run/stop lifecycle. Pings run before sampling, without static
ARP entries, and check connectivity without reporting RTT or throughput.

## Run locally

Use a Linux source checkout with p4net installed in a virtual environment, plus
`p4c`, `simple_switch_grpc`, `ip` and `ping` on `PATH`. Root is required for
namespaces and process memory inspection.

From the repository root, first check the script and native dependencies:

```bash
sudo env "PATH=$PATH" .venv/bin/python tools/benchmark.py \
  --check --switches 1 2 --output .cache/benchmarks/check
```

`--check` runs one trial per size/cache combination, verifies cache behavior,
both-direction connectivity, process-memory access and resource cleanup. It
does not save performance samples, timings or a summary table.

For a baseline run, close CPU- or memory-intensive applications and wait for
background activity to settle. If you restart the machine, let startup tasks
finish before collecting data. Keep the power mode, WSL resource limits and
other workload conditions consistent between runs.

```bash
sudo env "PATH=$PATH" .venv/bin/python tools/benchmark.py \
  --switches 1 2 4 8 --trials 5 \
  --idle-seconds 5 --interval 0.25 --settle 1 \
  --output .cache/benchmarks/baseline
```

The output directory must be new. Trial logs and results remain there if a later
trial fails. The script requests teardown on errors or coordinator exit and
checks for remaining topology processes, namespaces and switch interfaces
after normal shutdown.

## Test rig

`environment.json` records the system timestamp, Git revision and working-tree
status, script and P4 source hashes, Python and dependency versions, p4c/BMv2
versions, Linux/OS details, CPU information and affinity, memory information,
load average and measurement settings. Keep this file with the results.

WSL2 results describe that WSL2 setup. Use measurements from the intended host
when estimating another machine's startup time or memory needs.

The baseline below was collected on 2026-10-23 from runtime checkout
[`b2ebbc9`](https://github.com/zhh2001/p4net/commit/b2ebbc964041b30b66b8d089e2d157faea9f1595)
(package version `1.7.0`).

| Item | Recorded value |
| --- | --- |
| Collection window | 2026-10-23 01:48–01:54, UTC+08:00 |
| CPU | Intel Core i5-13500H, 16 logical CPUs available to the process |
| Memory | 7.75 GiB visible to WSL2 |
| System | Ubuntu 24.04.5 LTS, Linux 6.6.87.2-microsoft-standard-WSL2 |
| Python | 3.12.3 |
| Native tools | p4c 1.2.5.10 (`8b6de3c57`, Release), BMv2 `1.15.0-2bdd0b7b` |
| Libraries | grpcio 1.84.0, pyroute2 0.9.6, protobuf 7.36.2, p4runtime 1.6.0 |

## Results

`results.json` contains all completed trials. Each trial directory also contains
its own `result.json`, topology names, compiler artifacts and BMv2/worker logs.
The result includes per-process resource snapshots and both-direction ping
outcomes. A failed trial stops the run instead of contributing a partial result
to the summary.

`summary.md` reports the median startup time, shutdown time, idle CPU percentage
and mean sampled memory across the five trials for each size/cache combination.
Keep the raw samples when publishing the table so variation can be inspected.

All 40 trials completed, with all 300 directional ping checks passing. Each trial
produced 21 resource snapshots over an actual idle window of 5.001–5.012 seconds.
No trials were excluded, and each topology passed its cleanup checks.

| Switches | Hosts | Cache | Trials | Start (s) | Stop (s) | Idle CPU (%) | PSS (MiB) | RSS sum (MiB) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 2 | empty | 5 | 0.475 | 0.206 | 0.4 | 82.4 | 97.4 |
| 1 | 2 | warm | 5 | 0.264 | 0.200 | 0.4 | 82.4 | 97.4 |
| 2 | 4 | empty | 5 | 0.735 | 0.429 | 1.0 | 96.0 | 138.1 |
| 2 | 4 | warm | 5 | 0.530 | 0.411 | 1.0 | 95.9 | 138.1 |
| 4 | 8 | empty | 5 | 1.250 | 0.896 | 1.8 | 121.9 | 219.6 |
| 4 | 8 | warm | 5 | 1.011 | 0.856 | 1.6 | 121.8 | 219.5 |
| 8 | 16 | empty | 5 | 2.278 | 1.714 | 2.8 | 173.0 | 382.5 |
| 8 | 16 | warm | 5 | 2.079 | 1.658 | 2.6 | 173.0 | 382.5 |

Values are medians across five trials. Memory columns use each trial's mean
sampled usage. CPU uses 100% per logical CPU; on this system, one CPU tick over
the five-second window corresponds to about 0.2 percentage points.

Startup times for the one-switch empty-cache group ranged from 0.449 to 0.795 s.
The eight-switch warm-cache group ranged from 2.004 to 2.142 s, with shutdowns
from 1.588 to 1.833 s. The [raw baseline archive](assets/benchmarks/2026-10-23-b2ebbc9-py312.zip)
contains all trials, environment information, lifecycle ranges, logs, the exact
script and P4 source, and a SHA-256 manifest.

## Observations

In this run, warm-cache median startup times were 0.20–0.24 s lower than their
empty-cache counterparts. The eight-switch warm-cache topology started in
2.079 s and stopped in 1.658 s, with 173.0 MiB of mean sampled PSS and 2.6% idle
CPU usage at the median. These figures provide a reference for this host and
workload; larger topologies require their own measurements.

BMv2 shutdown currently waits up to five seconds after `SIGTERM`, then uses
`SIGKILL` if needed. This is a timeout, not an expected shutdown duration.

## Caveats

The baseline sends only connectivity probes. It does not measure throughput,
loss under load, RTT distributions or switch processing latency. The P4 program,
table contents, BMv2 build, logging, packet capture and traffic pattern affect
forwarding performance. See [Known limitations](known-limitations.md) and
[BMv2's performance notes](https://github.com/p4lang/behavioral-model/blob/main/docs/performance.md)
when designing a traffic benchmark.
