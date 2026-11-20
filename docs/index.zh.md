---
description: P4Net 是一个在 Linux 上运行 BMv2 和 P4Runtime 实验的 Python 框架。
---

# P4Net

P4Net 是一个在 Linux 上运行 BMv2 和 P4Runtime 实验的 Python 框架，可用于 P4 教学、开发控制器原型和验证转发逻辑。

P4Net 用 `veth` 接口对连接节点。每台主机有独立的 Linux 网络命名空间。BMv2 软件交换机与 `p4net` 进程位于同一个命名空间，通常是系统的根网络命名空间。

P4Net 通过 P4Runtime 控制 BMv2 交换机，支持 P4Runtime 的硬件交换机也使用同一套控制协议。实验测得的吞吐量和时延反映的是 BMv2 软件转发和 Linux 实验环境。详见[已知限制](known-limitations.md)。

## 安装

按[安装指南](getting-started/installation.md)配置 p4c、BMv2 和 `p4net` 命令行。

## 试跑 L3 转发

安装 CLI 后，克隆仓库并启动示例。

```sh
git clone https://github.com/zhh2001/p4net.git
cd p4net
sudo p4net examples/l3_forwarding/topology.py
```

在 `p4net>` 提示符下查看交换机的 IPv4 路由，再验证主机之间的连通性。

```text
s1 table dump MyIngress.ipv4_lpm
pingall
```

转发表中有两条 `/32` 路由，`pingall` 会显示 `2/2 succeeded`。输入 `exit` 停止拓扑。P4 程序和启动时的配置见 [L3 转发示例](examples/l3-forwarding.md)。

## 为什么选择 P4Net

- **一条命令接通 BMv2 和 P4Runtime。** P4Net 用 `p4c` 编译 P4 程序，启动 BMv2 并等待交换机就绪，再由内置的 P4Runtime 客户端连接交换机并下发流水线。
- **调试和测试共用一份拓扑。** CLI 在进入交互模式前调用可选的 `setup(net)` 函数。Python 测试可以复用同一份拓扑和初始化函数，用 `Network` 上下文管理器负责启停和清理。
- **在一个命令行里调试主机和交换机。** 执行主机命令、检查连通性后，可以直接通过 P4Runtime 下发表项、读计数器、接收 PacketIn 或发送 PacketOut。详见 [CLI 参考](cli.md)。
- **并发配置多台交换机。** 异步 P4Runtime 示例用一个 Python 事件循环向三台 BMv2 交换机下发九条转发表项。详见[异步并发示例](examples/async-concurrent.md)。
- **复现实验的链路条件。** 为主机配置 IPv4、IPv6 地址，按链路方向设置带宽、时延、抖动和丢包率，还可以在交换机端口抓包。
- **导出拓扑图。** 将拓扑保存为 DOT 文件，或通过 Graphviz 生成 SVG、PNG 或 PDF。

## Python 示例

先[在虚拟环境中安装 p4net](getting-started/installation.md#project-environments)。在项目目录下，把[快速上手中的 P4 程序](getting-started/quickstart.md#创建两个文件)保存为 `port_swap.p4`，再把下面的代码保存为 `demo.py`。

```python
from pathlib import Path

from p4net import Network
from p4net.topo import Topology

topo = Topology()
h1 = topo.add_host("h1", ip="10.0.0.1/24")
h2 = topo.add_host("h2", ip="10.0.0.2/24")
s1 = topo.add_switch("s1", p4_src=Path(__file__).with_name("port_swap.p4"))
topo.add_link(h1, s1, port_b=1)
topo.add_link(h2, s1, port_b=2)

with Network(topo) as net:
    print(net.ping("h1", "h2"))
```

在项目目录下，用虚拟环境中的 Python 运行脚本。

```sh
sudo .venv/bin/python demo.py
```

`h1` 收到 `h2` 的应答后，脚本会打印 `True`。`with` 代码块结束时，网络会自动关闭。

## 指南与参考

- [快速上手](getting-started/quickstart.md)从头搭建一个双主机拓扑。
- [教程](tutorial.md)演示双栈转发、运行时表项配置、按方向设置链路时延，以及通过 CPU 端口收发数据包。
- [示例](examples/index.md)收录九个可运行的拓扑，包括带内网络遥测和多交换机并发配置。
- [架构](architecture.md)说明各个 Python 包的职责和调用关系。
- [API 参考](api/network.md)列出 Python 接口及用法。

## 项目状态

P4Net **v1.10.0** 为稳定版本。参见[变更日志](changelog.md)、[API 稳定性约定](api-stability.md)和[路线图](roadmap.md)。
