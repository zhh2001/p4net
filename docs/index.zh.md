---
description: 在 Linux 上用 Python、BMv2 软件交换机和 P4Runtime 运行 P4 网络实验。
---

# p4net

p4net 是一个在 Linux 上运行 P4 网络实验的 Python 框架，可用于教学实验、开发 P4Runtime 控制器原型，以及验证 P4 程序的转发逻辑。

p4net 用 `p4c` 编译 P4 程序，用 `veth` 接口对连接节点。每台主机有独立的 Linux 网络命名空间。BMv2 软件交换机与 p4net 进程位于同一个命名空间，通常是系统的根网络命名空间。

p4net 通过 P4Runtime 控制 BMv2 交换机，支持 P4Runtime 的硬件交换机也使用同一套控制协议。实验测得的吞吐量和时延反映的是 BMv2 软件转发和 Linux 实验环境。详见[已知限制](known-limitations.md)。

## 安装

按[安装指南](getting-started/installation.md)配置 p4c、BMv2 和 p4net 命令行，再跟随[快速上手](getting-started/quickstart.md)运行第一个拓扑。

## 能做什么

- **用 Python 搭建拓扑。** 为主机配置 IPv4、IPv6 地址并连接交换机，按链路方向设置带宽、时延、抖动或丢包率。
- **配置交换机。** 使用同步或异步 P4Runtime 客户端下发表项、读取间接计数器、管理组播组、接收 PacketIn 和发送 PacketOut。
- **检查运行中的网络。** 在[交互式命令行](cli.md)里执行主机命令、检查连通性和查看交换机表项。
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

- [教程](tutorial.md)演示双栈转发、运行时表项配置、按方向设置链路时延，以及通过 CPU 端口收发数据包。
- [示例](examples/index.md)收录九个可运行的拓扑，包括带内网络遥测和多交换机并发配置。
- [架构](architecture.md)说明各个 Python 包的职责和调用关系。
- [API 参考](api/network.md)列出 Python 接口及用法。

## 项目状态

p4net **v1.10.0** 为稳定版本。参见[变更日志](changelog.md)、[API 稳定性约定](api-stability.md)和[路线图](roadmap.md)。
