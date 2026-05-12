---
description: p4net 是一个 Python 框架，用 Linux 网络命名空间承载真实的 BMv2 数据平面，通过 P4Runtime 控制，提供类似 Mininet 的声明式 API。
---

# p4net

> Python 实现的 Mininet 风格 P4 SDN 沙盒。

## 它是什么

p4net 在 Linux 网络命名空间中拉起真实的 `simple_switch_grpc`（BMv2）
数据平面，用 `veth` 对将主机和交换机相连，用 `tc netem` 注入链路损伤，
然后通过 P4Runtime 进行控制——这与真实硬件 P4 控制器的工作方式完全一致。
Python API 的形态参考 Mininet：声明一个由 `Host`、`P4Switch`、`Link`
组成的 `Topology`，交给 `Network`，就能拿到一个运行中的实验环境。

该框架面向 P4 开发与教学场景：课堂实验、控制器原型、转发逻辑回归测试，
以及任何更看重「几秒钟就能起一个多主机拓扑」而非「精确模拟某款交换机
ASIC」的实验。工具链由 `p4c`、`simple_switch_grpc` 和内核命名空间原语
组成；不依赖 Docker、OpenFlow，也不需要 Open vSwitch。

## 包含哪些组件

- **拓扑 DSL** —— `Host`、`P4Switch`、`Link`、`Topology`，构造期即做
  静态校验（接口名长度、IP 冲突、对称与非对称损伤的相互排斥关系）。
- **基于 p4c 的编译**，配套位于 `~/.cache/p4net/compiler/` 的内容
  寻址缓存——只在源码或参数发生变化时才重新编译。
- **BMv2 生命周期管理** —— 启动、就绪等待、停止、gRPC 端口分配，
  以及 atexit / signal 兜底清理。
- **P4Runtime gRPC 客户端** —— 表项编程、间接计数器读取、组播组管理，
  以及通过 StreamChannel 进行的 CPU 端口数据包 I/O。
- **基于 `tc netem` 的逐方向链路损伤** —— `bandwidth`、`delay`、
  `jitter`、`loss_pct` 既可对称配置，也可拆成 `*_a_to_b` /
  `*_b_to_a` 两个方向，或者在对称基础上叠加 `*_extra`。
- **IPv4 与 IPv6 主机寻址**，每个接口具备独立的 sysctl 门控
  （未显式启用 IPv6 的接口不会自动生成 link-local 地址）。
- **交互式 CLI** —— `p4net <topology.py>` 启动一个 Shell，提供表内容
  转储、ping 矩阵、数据包发送/监听、拓扑可视化等命令。
- **拓扑可视化**，由 `Topology.to_graphviz()` 与
  `Topology.render_graphviz()` 提供，输出 DOT、PNG、SVG、PDF 等格式。
- **统一的 `p4net.*` logger 层次**，附带级别约定与 CLI 详细度开关
  （`-v` / `-vv`）；参见[日志](logging.md)。

## 安装

```
pip install p4net
```

详细步骤包括外部依赖 `p4c` 与 BMv2，请参见
[安装](getting-started/installation.md)。

## 30 秒示例

```python
from pathlib import Path
from p4net import Network
from p4net.topo import Topology

topo = Topology()
h1 = topo.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topo.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topo.add_switch("s1", p4_src=Path("port_swap.p4"))
topo.add_link(h1, s1, port_b=1)
topo.add_link(h2, s1, port_b=2)

with Network(topo) as net:
    h1, h2 = net.host("h1"), net.host("h2")
    h1.exec(["ip", "neigh", "replace", "10.0.0.2",
             "lladdr", "00:00:00:00:00:02",
             "dev", "h1-eth0", "nud", "permanent"])
    print(h1.ping(h2))   # True
```

配套的 `port_swap.p4` 源码位于
[`examples/quick_start/`](examples/quick-start.md)。

## 接下来读什么

- **[快速上手](getting-started/quickstart.md)** —— 上面这个示例的逐步
  说明，附带 P4 程序源码。
- **[教程](tutorial.md)** —— 一个更长的讲解，覆盖双栈转发、运行时
  表项编程、非对称链路损伤、CPU 端口数据包 I/O。
- **[架构](architecture.md)** —— 各包的职责与背后设计取舍。
- **[示例](examples/index.md)** —— 七个可运行的拓扑，从端口翻转、
  L3 转发，一直到带内网络遥测（INT）。
- **[CLI 参考](cli.md)** —— 每条 Shell 命令的说明与示例输出。
- **[API 参考](api/network.md)** —— 由 Python docstring 自动生成。

## 项目状态

p4net 当前为 **v1.5.0**——稳定版本。公共 API 已按
[API 稳定性](api-stability.md)做出承诺：标为稳定的符号在 1.x 内不会
被破坏。补丁版本只修 bug；小版本以追加方式新增功能。在 v1.0.0
稳定性基础之上：v1.1.0 加入了单交换机 INT 示例、统一的 `p4net.*`
logger 层次与 CLI 详细度控制、按方向叠加的链路损伤；v1.2.0 在
`P4RuntimeClient` 上加入了寄存器读写 API，并据此把 INT 示例中的
`switch_id` 改成运行时可配置；v1.3.0 加入了多跳 INT 示例，演示
跨两台交换机的生产形态堆叠遥测；v1.4.0 加入了
`RunningSwitch.boot_timestamp_us`，把每台交换机的 BMv2 时间戳对齐到
挂钟——多跳 INT 示例现在能上报真实的逐跳转发延迟；v1.5.0 新增便利
封装 `Network.boot_timestamps`，并让多跳 INT 示例的协调文件路径可由
`P4NET_INT_BOOT_TIMES_PATH` 环境变量指定，使同主机上的并行拓扑
互不干扰。完整版本历史见 [变更日志](changelog.md)；已知边界见
[已知限制](known-limitations.md)；后续计划见 [路线图](roadmap.md)。

## 许可证

[Apache-2.0](https://github.com/zhh2001/p4net/blob/main/LICENSE)。
