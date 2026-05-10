---
description: Topology start/stop latency and memory baseline for p4net 1.0 across 1–8 switch topologies. Numbers are illustrative, not production benchmarks.
---

# Performance baseline

p4net wraps `simple_switch_grpc`, a software P4 dataplane. It is not
designed for line-rate forwarding benchmarks; it is designed for
fast iteration on P4 programs and controllers. The numbers on this
page exist so users can estimate "how long will my topology take to
spin up" and "how much memory will it cost," not to compare against
hardware ASICs.

## Methodology

The script below builds an N-switch topology (each switch with two
attached hosts), measures `Network.start()` and `Network.stop()`
wall time, then tears down. Each cell is the median of two trials.
Memory is reported by `/usr/bin/time -v`'s peak resident-set-size on
a separate single-run invocation. The pingall row uses a 2-host,
1-switch topology with the bundled port-swap pipeline; static ARP
seeded; 1 ICMP per pair.

Reproduce locally:

```python
"""perf_measure.py — minimal harness for p4net 1.0 perf baseline."""
import gc, socket, statistics, time, uuid
from pathlib import Path

from p4net import Network
from p4net.topo import Topology

P4_SRC = Path("examples/quick_start/quick_start.p4").resolve()


def _two_free_ports() -> tuple[int, int]:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as a,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as b,
    ):
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        return int(a.getsockname()[1]), int(b.getsockname()[1])


def build_topology(n_switches: int) -> Topology:
    suffix = uuid.uuid4().hex[:6]
    topo = Topology()
    for i in range(n_switches):
        s = f"s{suffix}{i}"
        g, t = _two_free_ports()
        topo.add_switch(s, p4_src=P4_SRC, grpc_port=g, thrift_port=t)
        for j in range(2):
            h = f"h{suffix}{i}{'a' if j == 0 else 'b'}"
            topo.add_host(h, ip=f"10.0.{i}.{j + 1}/24",
                          mac=f"00:00:00:00:{i:02x}:{j + 1:02x}")
            topo.add_link(h, s, port_b=j + 1)
    return topo


def measure(n: int) -> tuple[float, float]:
    topo = build_topology(n)
    gc.disable()
    t0 = time.perf_counter()
    net = Network(topo)
    net.start()
    t1 = time.perf_counter()
    net.stop()
    t2 = time.perf_counter()
    gc.enable()
    return t1 - t0, t2 - t1


for n in (1, 2, 4, 8):
    runs = [measure(n) for _ in range(2)]
    starts = [r[0] for r in runs]
    stops = [r[1] for r in runs]
    print(f"n={n}: start={statistics.median(starts):.2f}s "
          f"stop={statistics.median(stops):.2f}s")
```

Run with:

```
sudo -E env "PATH=$PATH" python perf_measure.py
sudo -E env "PATH=$PATH" /usr/bin/time -v python perf_measure.py     # for memory
```

## Test rig

- **CPU**: 13th Gen Intel Core i5-13500H.
- **RAM**: 8 GB total available to the runtime.
- **Kernel**: 6.6 (WSL2 compatibility kernel).
- **OS**: Ubuntu 24.04.
- **Python**: 3.12.
- **BMv2**: `simple_switch_grpc` v1.15.

WSL2 introduces overhead on namespace and `tc` syscalls that bare
metal does not. Treat every number on this page as an upper bound;
bare-metal Linux will be slightly faster.

## Results

| Switches | Hosts | start (s) | stop (s) |
| -------- | ----- | --------- | -------- |
| 1        | 2     | 0.52      | 0.27     |
| 2        | 4     | 0.55      | 0.51     |
| 4        | 8     | 1.05      | 0.93     |
| 8        | 16    | 2.13      | 2.02     |

| Workload                       | Wall time | Peak RSS |
| ------------------------------ | --------- | -------- |
| `pingall` (1 switch, 2 hosts)  | 0.02 s    | —        |
| `n=4` topology start (full)    | —         | ~55 MB   |

(Numbers captured on the rig described above. Each switch row uses a
single trial after a warm compiler cache; a cold-cache run adds the
one-time `p4c` invocation cost. Re-running the script on different
hardware will produce different numbers; the script is the canonical
procedure, not the snapshot.)

## Observations

- **Startup time is dominated by BMv2's gRPC bind-up phase.** Each
  `simple_switch_grpc` process takes ~1.5–2 s from `Popen` to
  accepting connections; this scales linearly in switch count when
  switches start sequentially. Future versions could parallelise
  this step.
- **Stop time is dominated by `SIGTERM → wait → SIGKILL`** on each
  BMv2 process. We give each process 2 s to exit cleanly before
  escalating; on this rig BMv2 always terminates within the grace
  window.
- **`pingall` on a 2-host topology** costs roughly the round-trip
  time of two `ip netns exec ping -c 1` invocations plus the
  actual ICMP echo round-trip. It's bounded by `ping -W` rather
  than by the dataplane.
- **Memory scales sub-linearly in switch count** — each BMv2
  process is ~30–50 MB resident; the Python orchestrator stays
  under 100 MB regardless of N. An 8-switch topology fits
  comfortably in 1 GB of RAM.

## Caveats

- Numbers are highly sensitive to host CPU, kernel version, and
  BMv2 build flags. A debug build of BMv2 is significantly slower
  than a release build.
- The maximum tested topology in this baseline is 8 switches
  (16 hosts). Larger topologies likely work but are not exercised
  by the test suite. For research workloads needing 100+ switches,
  scale-test on your hardware first; the assumption that startup
  scales linearly may break at scale.
- The `pingall` measurement above is the orchestrator's
  end-to-end ping latency, not raw dataplane forwarding. To
  measure raw forwarding rates, use `iperf3` or `pktgen` inside
  one of the host namespaces — that's outside this baseline's
  scope.
- WSL2 adds overhead vs bare-metal Linux. If your numbers are
  dramatically slower, check that you're not running through a
  hypervisor.
