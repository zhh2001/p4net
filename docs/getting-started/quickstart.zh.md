---
description: 创建一台交换机和两台主机，用端口互换 P4 程序验证双向连通。
---

# 快速上手

本例创建两台 Linux 主机和一台 BMv2 交换机，最终用 `pingall` 验证双向连通。请先完成[安装](installation.md)，并在已安装 p4net 的 Python 环境中执行下面的命令。

```text
h1 (10.0.0.1/24) ---- 1 [ s1 ] 2 ---- h2 (10.0.0.2/24)
```

交换机把端口 1 收到的帧转发到端口 2，反向同理。ARP 和 ICMP 都经过这条路径，因此主机会自动学习对方的 MAC 地址，无需静态邻居表或运行时表项。

## 创建两个文件

先创建一个工作目录：

```bash
mkdir -p p4net-demo
cd p4net-demo
```

将下面的 Python 代码保存为 `quickstart.py`：

```python
from pathlib import Path

from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()
h1 = topology.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topology.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topology.add_switch("s1", p4_src=HERE / "port_swap.p4")
topology.add_link(h1, s1, port_b=1)
topology.add_link(h2, s1, port_b=2)
```

`port_b=1` 和 `port_b=2` 分别指定两条链路在交换机上的端口号。主机侧使用接口 `h1-eth0` 和 `h2-eth0`。`HERE` 让 P4 源文件相对于 `quickstart.py` 定位。

将下面的 P4 程序保存为同一目录下的 `port_swap.p4`：

```p4
#include <core.p4>
#include <v1model.p4>

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

struct headers {
    ethernet_t ethernet;
}

struct metadata {}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        pkt.extract(hdr.ethernet);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyIngress(inout headers hdr, inout metadata meta,
                  inout standard_metadata_t std) {
    apply {
        if (std.ingress_port == 1) {
            std.egress_spec = 2;
        } else if (std.ingress_port == 2) {
            std.egress_spec = 1;
        } else {
            mark_to_drop(std);
        }
    }
}

control MyEgress(inout headers hdr, inout metadata meta,
                 inout standard_metadata_t std) { apply {} }

control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyDeparser(packet_out pkt, in headers hdr) {
    apply {
        pkt.emit(hdr.ethernet);
    }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(),
         MyComputeChecksum(), MyDeparser()) main;
```

`MyIngress` 按入口端口决定出口，其他端口收到的帧被丢弃。这个程序没有匹配表，启动时只需下发流水线。

## 启动网络

在两个文件所在的目录执行：

```bash
sudo "$(command -v p4net)" quickstart.py
```

该命令使用当前环境中 `p4net` 的路径，适用于虚拟环境安装。CLI 读取模块级的 `topology` 变量，编译 P4 程序、创建主机命名空间和链路、启动 BMv2，并通过 P4Runtime 下发流水线。完成后显示 `p4net>` 提示符。

若拓扑文件定义了 `setup(net)`，CLI 会在网络启动后、进入 Shell 前调用它。本例不需要额外初始化。

## 验证连通

在 `p4net>` 后依次输入 `hosts` 和 `pingall`：

```text
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  -           h1-eth0
h2    10.0.0.2/24  -           h2-eth0

p4net> pingall
H \ H h1  h2
h1  -   1
h2  1   -
2/2 succeeded
```

`hosts` 列出主机地址和接口。矩阵中的行是发送方、列是接收方，`1` 表示成功，`X` 表示失败，`-` 表示同一台主机。`2/2 succeeded` 表示 `h1` 到 `h2`、`h2` 到 `h1` 都已连通。

## 退出与清理

输入 `exit` 或在空提示符处按 `Ctrl-D`。p4net 会关闭控制连接、停止 BMv2，并删除本次创建的命名空间和链路。

如果编译、启动或连通验证失败，参见[故障排查](../troubleshooting.md)。

## 继续学习

- [教程](../tutorial.md)：学习运行时表项编程和更复杂的拓扑。
- [CLI 参考](../cli.md)：查看交互命令、参数和数据包操作。
- [示例目录](../examples/index.md)：运行更多拓扑，了解不同的数据平面功能。
