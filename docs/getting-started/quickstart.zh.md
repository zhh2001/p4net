---
description: 用不到 100 行代码完成一个端到端的双主机 p4net 拓扑，配套 P4 程序、运行命令和成功 ping 的输出。
---

# 快速上手

本页演示最小的可用 p4net 程序——一台交换机、两台主机、一个端口
互换的数据平面，最终成功 ping 通。如果你尚未安装 p4net 及其外部
依赖，请先阅读[安装](installation.md)。

## 你将构建的内容

```
+----+         +----+         +----+
| h1 |--eth0---|    |---eth0--| h2 |
+----+   1    | s1 |    2    +----+
              +----+
```

`h1` 与 `h2` 是位于私有网络命名空间中的 Linux 主机；`s1` 是一个
运行端口互换流水线的 `simple_switch_grpc` 进程：从端口 1 进来的
数据包从端口 2 出去，反之亦然。不需要运行时表项编程——数据平面
逻辑是写死在 P4 程序里的。

## Python 拓扑

保存为 `quickstart.py`：

```python
"""Minimal two-host p4net topology."""
from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent

topology = Topology()
h1 = topology.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topology.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topology.add_switch("s1", p4_src=HERE / "port_swap.p4")
topology.add_link(h1, s1, port_b=1)
topology.add_link(h2, s1, port_b=2)


def setup(net: Network) -> None:
    """Pre-seed static ARP so the first ICMP doesn't have to resolve."""
    h1, h2 = net.host("h1"), net.host("h2")
    h1.exec(["ip", "neigh", "replace", "10.0.0.2",
             "lladdr", "00:00:00:00:00:02",
             "dev", "h1-eth0", "nud", "permanent"])
    h2.exec(["ip", "neigh", "replace", "10.0.0.1",
             "lladdr", "00:00:00:00:00:01",
             "dev", "h2-eth0", "nud", "permanent"])
```

`topology = Topology()` 加上 `def setup(net)` 是 `p4net` 控制台脚本
约定的形态：模块级的 `topology` 变量加上可选的 `setup(net)`，后者会
在网络启动后被调用。直接构造 `Network` 对象的纯 Python 脚本同样
可以。

## P4 程序

保存为 `port_swap.p4`，与 `quickstart.py` 放在同一目录：

```p4
#include <core.p4>
#include <v1model.p4>

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

struct headers { ethernet_t ethernet; }
struct metadata {}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start { pkt.extract(hdr.ethernet); transition accept; }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyIngress(inout headers hdr, inout metadata meta,
                  inout standard_metadata_t std) {
    apply {
        if (std.ingress_port == 1) { std.egress_spec = 2; }
        else if (std.ingress_port == 2) { std.egress_spec = 1; }
        else { mark_to_drop(std); }
    }
}

control MyEgress(inout headers hdr, inout metadata meta,
                 inout standard_metadata_t std) { apply {} }

control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyDeparser(packet_out pkt, in headers hdr) {
    apply { pkt.emit(hdr.ethernet); }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(),
         MyComputeChecksum(), MyDeparser()) main;
```

## 运行

```
sudo p4net quickstart.py
```

编排器会校验拓扑、编译 `port_swap.p4`、创建命名空间、连通 veth 对、
配置地址、在 root 命名空间启动 `simple_switch_grpc`、通过 P4Runtime
推送编译好的流水线、运行 `setup(net)` 注入 ARP，然后进入交互式
Shell。

在 Shell 中：

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  -            h1-eth0
h2    10.0.0.2/24  -            h2-eth0

p4net> pingall
H \ H   h1   h2
   h1    -    1
   h2    1    -
2/2 succeeded
```

按 `Ctrl-D`（或输入 `exit`）即可拆掉整个网络。

## 刚才发生了什么

按顺序：

1. `Topology.validate()` 运行（接口名长度、IP 冲突、对称/非对称
   损伤的相容性等）。
2. `P4Compiler` 检查内容寻址缓存；缓存未命中时，调用
   `p4c -b bmv2 --p4runtime-files=...`。
3. 创建网络命名空间 `h1` 与 `h2`。
4. 在 root 命名空间创建两对 veth：`h1-eth0 ↔ s1-eth1` 与
   `h2-eth0 ↔ s1-eth2`，再把主机侧的一端搬入对应命名空间。
5. 应用 IPv4 地址、MAC 覆盖、按接口的 IPv6 sysctl 门控；接口拉起。
6. 启动 `simple_switch_grpc`，参数 `--grpc-server-addr
   127.0.0.1:50051`；编排器轮询 gRPC 端口直到可连，然后通过
   `SetForwardingPipelineConfig` 推送流水线。
7. `setup(net)` 注入静态 ARP。
8. CLI 接管。退出时，上面每一步按相反顺序回卷——通过上下文
   管理器显式触发，或在异常路径上由 atexit 兜底处理。

每一步的细节见[架构](../architecture.md)。

## 接下来读什么

- [教程](../tutorial.md)演示一个 4 主机、2 交换机的双栈拓扑，包含
  运行时表项编程、非对称链路损伤、CPU 端口数据包 I/O。
- [示例目录](../examples/index.md)收录了六个可运行的拓扑，分别
  覆盖每一项 v0.2.0 特性。
- [CLI 参考](../cli.md)记录了每条 Shell 命令。
