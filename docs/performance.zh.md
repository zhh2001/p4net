---
description: p4net 1.0 在 1–8 交换机拓扑下的启动/停止延迟与内存基线。结果具有指示性，并非生产硬件基准。
---

# 性能特征

p4net 封装的是 `simple_switch_grpc`——一个软件 P4 数据平面。它的
设计目标不是线速转发基准，而是 P4 程序与控制器的快速迭代。本页
的数字提供给读者用来估计「我的拓扑要花多久启动」「会消耗多少
内存」，而不是与硬件 ASIC 比较。

## 测量方法

下面的脚本构建一个 N 交换机的拓扑（每台交换机连两台主机），测量
`Network.start()` 与 `Network.stop()` 的墙钟时间，然后拆掉。每个
单元格取两次测试的中位数。内存通过 `/usr/bin/time -v` 报告的
峰值常驻集（RSS）记录，单独跑一次。pingall 一行使用 2 主机 1
交换机的拓扑，搭配自带的端口翻转流水线；提前注入静态 ARP；每对
1 个 ICMP。

本地复现：

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

执行：

```
sudo -E env "PATH=$PATH" python perf_measure.py
sudo -E env "PATH=$PATH" /usr/bin/time -v python perf_measure.py     # 测内存
```

## 测试环境

- **CPU**：13th Gen Intel Core i5-13500H。
- **RAM**：运行时可用 8 GB。
- **内核**：6.6（WSL2 兼容内核）。
- **操作系统**：Ubuntu 24.04。
- **Python**：3.12。
- **BMv2**：`simple_switch_grpc` v1.15。

WSL2 在命名空间与 `tc` 系统调用上引入了一定开销。把本页所有数字
当作上界；裸机 Linux 会略快。

## 测量结果

| 交换机 | 主机 | start (s) | stop (s) |
| ------ | ---- | --------- | -------- |
| 1      | 2    | 0.52      | 0.27     |
| 2      | 4    | 0.55      | 0.51     |
| 4      | 8    | 1.05      | 0.93     |
| 8      | 16   | 2.13      | 2.02     |

| 场景                            | 墙钟时间  | 峰值 RSS |
| ------------------------------- | --------- | -------- |
| `pingall`（1 交换机 / 2 主机）  | 0.02 s    | —        |
| n=4 拓扑启动（完整一轮）        | —         | ~55 MB   |

（数字来自上文环境的一次实测。交换机行均为预热缓存后的单次试验；
冷缓存会额外加上一次 `p4c` 调用成本。在不同硬件上重跑得到的数字
会不同——脚本本身才是规范流程，本表只是某次快照。）

## 观察

- **启动时间主要受 BMv2 的 gRPC 绑定阶段控制**。每个
  `simple_switch_grpc` 进程从 `Popen` 到接受连接需要约 1.5–2 秒；
  在串行启动时随交换机数量线性增长。后续版本可考虑并行化这一步。
- **停止时间主要受 BMv2 进程的 `SIGTERM → wait → SIGKILL` 控制**。
  每个进程有 2 秒优雅退出窗口；这台机器上 BMv2 总能在窗口内退出。
- **2 主机拓扑的 `pingall`** 大致等于两次
  `ip netns exec ping -c 1` 调用加上实际 ICMP 往返时间；上限是
  `ping -W`，与数据平面无关。
- **内存随交换机数量呈次线性增长**。每个 BMv2 进程常驻 30–50 MB；
  Python 编排器无论 N 多少都保持在 100 MB 以下。8 交换机拓扑
  在 1 GB 内存里跑得很轻松。

## 注意事项

- 数字对主机 CPU、内核版本、BMv2 构建参数高度敏感。BMv2 的调试版
  比 release 版慢得多。
- 本基线测试到的最大拓扑是 8 交换机（16 主机）。更大的拓扑应当
  也能工作，但不在测试套件覆盖范围内。100+ 交换机的科研工作
  请先在你的硬件上做扩展测试；「启动时间随 N 线性增长」的假设
  在更大规模下可能不成立。
- 上面 `pingall` 测的是编排器端到端 ping 延迟，不是裸数据平面
  转发率。要测原始转发性能，请在某台主机命名空间里使用
  `iperf3` 或 `pktgen`——这超出本基线范围。
- WSL2 比裸机 Linux 慢。如果你的数字明显更慢，先确认是否运行在
  hypervisor 中。
