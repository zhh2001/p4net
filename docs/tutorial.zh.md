---
description: 一个四主机两交换机的 p4net 教程，覆盖 IPv4 与 IPv6 LPM 转发、非对称链路损伤，以及 CPU 端口数据包 I/O。
---

# 教程

本教程构建的拓扑比[快速上手](getting-started/quickstart.md)更复杂：
四台主机、两台交换机，IPv4 与 IPv6 双栈，单向链路延迟，以及一条
让控制器观察未匹配数据包的 CPU 端口 punt 路径。一份程序里覆盖
v0.2.0 的全部特性。

如果你尚未安装 p4net 及其外部依赖，请先阅读
[安装](getting-started/installation.md)。

## 我们要构建什么

```
                 +----+
                 | h1 | 10.0.0.1/24, fd00::1/64
                 +-+--+
                   | （h1↔s1 链路上设置 delay_a_to_b="50ms"）
                 +-+--+        +----+
                 | s1 |--------| s2 |
                 +-+--+        +-+--+
                   |             |
                 +-+--+        +-+--+
        h2/h3 -- |    |        |    | -- h4
                 ...分别接到 s1 / s2...
```

具体来说：

- `h1` 与 `h2` 接到 `s1`；`h1` 的链路上设置了 50ms 单向延迟
  （h1 → s1）。
- `h3` 与 `h4` 接到 `s2`。
- `s1` ↔ `s2` 是骨干链路，单向延迟 100ms（s1 → s2）。
- 每台主机都拥有同一个 `/24` 与 `/64` 上的 IPv4 与 IPv6 地址。
- 流水线通过 `ipv4_lpm` 表转发 IPv4，通过 `ipv6_lpm` 表转发
  IPv6——两张表都在运行时编程。未匹配的包通过 CPU 端口上送
  控制器。

## 步骤 1：搭建拓扑骨架

保存为 `tutorial.py`：

```python
"""p4net tutorial: dual-stack forwarding with asymmetric impairment."""
from pathlib import Path

from p4net import Network
from p4net.topo import Topology

HERE = Path(__file__).resolve().parent
P4_SRC = HERE / "tutorial.p4"

topology = Topology()

h1 = topology.add_host("h1", ip="10.0.0.1/24", ip6="fd00::1/64",
                       mac="00:00:00:00:00:01")
h2 = topology.add_host("h2", ip="10.0.0.2/24", ip6="fd00::2/64",
                       mac="00:00:00:00:00:02")
h3 = topology.add_host("h3", ip="10.0.0.3/24", ip6="fd00::3/64",
                       mac="00:00:00:00:00:03")
h4 = topology.add_host("h4", ip="10.0.0.4/24", ip6="fd00::4/64",
                       mac="00:00:00:00:00:04")

s1 = topology.add_switch("s1", p4_src=P4_SRC, cpu_port=510)
s2 = topology.add_switch("s2", p4_src=P4_SRC, cpu_port=510)
```

两台交换机加载同一份 `tutorial.p4`——编译缓存确保它只被编译一次。
`cpu_port=510` 是 BMv2 的 punt 约定：发往端口 510 的数据包以
`PacketIn` 形式上送控制器。

## 步骤 2：带非对称延迟的链路

```python
# 接到 s1 的主机。
topology.add_link(h1, s1, port_b=1, delay_a_to_b="50ms")
topology.add_link(h2, s1, port_b=2)
# 接到 s2 的主机。
topology.add_link(h3, s2, port_b=1)
topology.add_link(h4, s2, port_b=2)
# 骨干链路。
topology.add_link(s1, s2, port_a=3, port_b=3, delay_a_to_b="100ms")
```

h1↔s1 链路上的 `delay_a_to_b="50ms"` 把 `tc netem delay 50ms` 应用
到 a 侧 veth——也就是位于 `h1` 命名空间里的那一端。从 `h1` 出向
`s1` 的方向上累加 50ms；反向不整形。同理，s1↔s2 链路上的
`delay_a_to_b="100ms"` 只对 s1 → s2 方向生效。所以单向 `h1 → h3`
累计 50ms（h1→s1）+ 100ms（s1→s2）+ 0ms（s2→h3）= 150ms；反向
`h3 → h1` 不整形，因此 h1 到 h3 的 ping RTT 大约为 150ms，抖动
在亚毫秒级别。

## 步骤 3：P4 程序

保存为 `tutorial.p4`：

```p4
#include <core.p4>
#include <v1model.p4>

const bit<9> CPU_PORT = 510;

@controller_header("packet_in")
header packet_in_t { bit<9> ingress_port; bit<7> _pad0; }

@controller_header("packet_out")
header packet_out_t { bit<9> egress_port; bit<7> _pad0; }

header ethernet_t { bit<48> dstAddr; bit<48> srcAddr; bit<16> etherType; }
header ipv4_t {
    bit<4> version; bit<4> ihl; bit<8> diffserv; bit<16> totalLen;
    bit<16> identification; bit<3> flags; bit<13> fragOffset;
    bit<8> ttl; bit<8> protocol; bit<16> hdrChecksum;
    bit<32> srcAddr; bit<32> dstAddr;
}
header ipv6_t {
    bit<4> version; bit<8> trafficClass; bit<20> flowLabel;
    bit<16> payloadLen; bit<8> nextHdr; bit<8> hopLimit;
    bit<128> srcAddr; bit<128> dstAddr;
}

struct headers {
    packet_in_t  packet_in;
    packet_out_t packet_out;
    ethernet_t   ethernet;
    ipv4_t       ipv4;
    ipv6_t       ipv6;
}
struct metadata {}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        transition select(std.ingress_port) {
            CPU_PORT: parse_packet_out;
            default:  parse_ethernet;
        }
    }
    state parse_packet_out { pkt.extract(hdr.packet_out); transition parse_ethernet; }
    state parse_ethernet {
        pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            0x0800: parse_ipv4;
            0x86DD: parse_ipv6;
            default: accept;
        }
    }
    state parse_ipv4 { pkt.extract(hdr.ipv4); transition accept; }
    state parse_ipv6 { pkt.extract(hdr.ipv6); transition accept; }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyIngress(inout headers hdr, inout metadata meta,
                  inout standard_metadata_t std) {
    action drop() { mark_to_drop(std); }
    action set_egress_port(bit<9> port) { std.egress_spec = port; }
    action punt() {
        std.egress_spec = CPU_PORT;
        hdr.packet_in.setValid();
        hdr.packet_in.ingress_port = std.ingress_port;
        hdr.packet_in._pad0 = 0;
    }
    table ipv4_lpm {
        key = { hdr.ipv4.dstAddr: lpm; }
        actions = { drop; set_egress_port; punt; }
        default_action = punt();
        size = 1024;
    }
    table ipv6_lpm {
        key = { hdr.ipv6.dstAddr: lpm; }
        actions = { drop; set_egress_port; punt; }
        default_action = punt();
        size = 1024;
    }
    apply {
        if (std.ingress_port == CPU_PORT) {
            std.egress_spec = hdr.packet_out.egress_port;
            hdr.packet_out.setInvalid();
        } else if (hdr.ipv4.isValid()) {
            ipv4_lpm.apply();
        } else if (hdr.ipv6.isValid()) {
            ipv6_lpm.apply();
        }
    }
}

control MyEgress(inout headers hdr, inout metadata meta,
                 inout standard_metadata_t std) { apply {} }
control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyDeparser(packet_out pkt, in headers hdr) {
    apply {
        pkt.emit(hdr.packet_in);
        pkt.emit(hdr.ethernet);
        pkt.emit(hdr.ipv4);
        pkt.emit(hdr.ipv6);
    }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(),
         MyComputeChecksum(), MyDeparser()) main;
```

值得注意的几个点：

- 两张 LPM 表共用 `default_action = punt()`。两张表都未命中的包
  会以填好 `packet_in` 控制器报头的形式上送 CPU 端口。
- `set_egress_port(port)` 带参数，控制器为任意主机安装路由时
  无需为每台主机定义不同动作。
- 控制器注入的包（解析时走 `packet_out` 报头分支）会按
  `hdr.packet_out.egress_port` 转发，并在 deparse 之前把控制器
  报头标记无效，从而让线上包看起来正常。

## 步骤 4：编程表项与注入邻居

回到 `tutorial.py`：

```python
def setup(net: Network) -> None:
    """Pre-seed ARP/ND, install LPM entries on both switches."""
    h1 = net.host("h1")
    h2 = net.host("h2")
    h3 = net.host("h3")
    h4 = net.host("h4")

    # 静态 ARP/ND。四台主机在同一 /24 与 /64 内，每台都能直达
    # 其他三台，因此把全部组合都注入。
    arp_pairs = [
        (h1, "10.0.0.2", "00:00:00:00:00:02"),
        (h1, "10.0.0.3", "00:00:00:00:00:03"),
        (h1, "10.0.0.4", "00:00:00:00:00:04"),
        (h2, "10.0.0.1", "00:00:00:00:00:01"),
        (h2, "10.0.0.3", "00:00:00:00:00:03"),
        (h2, "10.0.0.4", "00:00:00:00:00:04"),
        (h3, "10.0.0.1", "00:00:00:00:00:01"),
        (h3, "10.0.0.2", "00:00:00:00:00:02"),
        (h3, "10.0.0.4", "00:00:00:00:00:04"),
        (h4, "10.0.0.1", "00:00:00:00:00:01"),
        (h4, "10.0.0.2", "00:00:00:00:00:02"),
        (h4, "10.0.0.3", "00:00:00:00:00:03"),
    ]
    for host, ip4, mac in arp_pairs:
        iface = next(iter(host.interfaces))
        host.exec(["ip", "neigh", "replace", ip4, "lladdr", mac,
                   "dev", iface, "nud", "permanent"])
    # IPv6 ND 镜像 ARP 条目。
    for host, ip4, mac in arp_pairs:
        ip6 = "fd00::" + ip4.split(".")[-1]
        iface = next(iter(host.interfaces))
        host.exec(["ip", "-6", "neigh", "replace", ip6, "lladdr", mac,
                   "dev", iface, "nud", "permanent"])

    # 转发表：s1 本地处理 h1/h2，h3/h4 走骨干（端口 3）；s2 镜像。
    s1 = net.switch("s1")
    s2 = net.switch("s2")

    for table_name, dst_prefix, port in [
        ("MyIngress.ipv4_lpm", "10.0.0.1/32", 1),
        ("MyIngress.ipv4_lpm", "10.0.0.2/32", 2),
        ("MyIngress.ipv4_lpm", "10.0.0.3/32", 3),
        ("MyIngress.ipv4_lpm", "10.0.0.4/32", 3),
    ]:
        s1.client.insert_table_entry(
            table=table_name,
            match={"hdr.ipv4.dstAddr": dst_prefix},
            action="MyIngress.set_egress_port",
            params={"port": port},
        )
    for table_name, dst_prefix, port in [
        ("MyIngress.ipv4_lpm", "10.0.0.1/32", 3),
        ("MyIngress.ipv4_lpm", "10.0.0.2/32", 3),
        ("MyIngress.ipv4_lpm", "10.0.0.3/32", 1),
        ("MyIngress.ipv4_lpm", "10.0.0.4/32", 2),
    ]:
        s2.client.insert_table_entry(
            table=table_name,
            match={"hdr.ipv4.dstAddr": dst_prefix},
            action="MyIngress.set_egress_port",
            params={"port": port},
        )
    # IPv6 同样的方案。
    for table_name, dst_prefix, port in [
        ("MyIngress.ipv6_lpm", "fd00::1/128", 1),
        ("MyIngress.ipv6_lpm", "fd00::2/128", 2),
        ("MyIngress.ipv6_lpm", "fd00::3/128", 3),
        ("MyIngress.ipv6_lpm", "fd00::4/128", 3),
    ]:
        s1.client.insert_table_entry(
            table=table_name,
            match={"hdr.ipv6.dstAddr": dst_prefix},
            action="MyIngress.set_egress_port",
            params={"port": port},
        )
    for table_name, dst_prefix, port in [
        ("MyIngress.ipv6_lpm", "fd00::1/128", 3),
        ("MyIngress.ipv6_lpm", "fd00::2/128", 3),
        ("MyIngress.ipv6_lpm", "fd00::3/128", 1),
        ("MyIngress.ipv6_lpm", "fd00::4/128", 2),
    ]:
        s2.client.insert_table_entry(
            table=table_name,
            match={"hdr.ipv6.dstAddr": dst_prefix},
            action="MyIngress.set_egress_port",
            params={"port": port},
        )


if __name__ == "__main__":
    from p4net.cli.main import main
    raise SystemExit(main([__file__]))
```

## 步骤 5：注册 packet-in 处理器（可选）

要观察 punt 路径，可以在进入 Shell 之前注册一个处理器。处理器跑
在 StreamChannel 消费者线程上，逻辑要短、要快。

```python
def setup(net: Network) -> None:
    # ...（上面所有代码）...
    def log_punt(payload: bytes, metadata: dict[str, int]) -> None:
        port = metadata.get("ingress_port", "?")
        # 截断以便日志可读。
        head = payload[:32].hex()
        net.host("h1").exec(  # 通过 h1 命名空间里的 stdout 记录
            ["logger", "-t", "p4net-punt", f"port={port} head={head}"])
    net.switch("s1").client.on_packet_in(log_punt)
    net.switch("s2").client.on_packet_in(log_punt)
```

生产环境的控制器会解析 punt 上来的以太网帧、做出决策，并通过
`send_packet_out` 注入回去。本例只是把事件记下来。

## 步骤 6：运行和探索

```
sudo p4net tutorial.py
```

Shell 里：

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  fd00::1/64   h1-eth0
h2    10.0.0.2/24  fd00::2/64   h2-eth0
h3    10.0.0.3/24  fd00::3/64   h3-eth0
h4    10.0.0.4/24  fd00::4/64   h4-eth0

p4net> pingall
H \ H   h1   h2   h3   h4
   h1    -    1    1    1
   h2    1    -    1    1
   h3    1    1    -    1
   h4    1    1    1    -
12/12 succeeded

p4net> pingall6
H \ H   h1   h2   h3   h4
   h1    -    1    1    1
   ...
12/12 succeeded

p4net> s1 table dump MyIngress.ipv4_lpm
#0
  table:    MyIngress.ipv4_lpm
  match:    {'hdr.ipv4.dstAddr': '10.0.0.1/32'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
#1
  ...

p4net> s1 table dump MyIngress.ipv6_lpm
#0
  table:    MyIngress.ipv6_lpm
  match:    {'hdr.ipv6.dstAddr': 'fd00::1/128'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
  ...
```

注意 IPv6 LPM 的 match 渲染为 `fd00::1/128`，而不是裸字节。这是
`P4InfoIndex.decode_match` 通过 `decode_ipv6` 完成的字节-字符串
往返。

## 步骤 7：测量非对称链路

`h1 → h3` 经过两段被整形的链路：50ms（h1 → s1）加 100ms
（s1 → s2）。反向无整形。所以 ping RTT 约 150ms。

```
p4net> h1 ping h3 5 3
PING 10.0.0.3 (10.0.0.3) 56(84) bytes of data.
64 bytes from 10.0.0.3: icmp_seq=1 ttl=64 time=151 ms
64 bytes from 10.0.0.3: icmp_seq=2 ttl=64 time=151 ms
...
rtt min/avg/max/mdev = 151.012/151.187/151.412/0.392 ms
```

`h2 → h3` 在 h2 一侧无整形，只穿过 100ms 的骨干，因此 RTT
约 100ms；`h1 → h2` 仅在 h1 出口被整形 50ms，RTT 约 50ms。

## 步骤 8：可视化

```
p4net> topology graph /tmp/topo.png
/tmp/topo.png
```

编排器先调用 `Topology.validate()`，然后把 DOT 源码喂给
`dot -Tpng`。在图片查看器中打开 `/tmp/topo.png` 即可看到渲染
结果。

如果未安装 `dot`，可以直接输出 DOT 源码：

```
p4net> topology graph /tmp/topo.dot format=dot
/tmp/topo.dot
```

## 接下来读什么

- [示例目录](examples/index.md)收录了六个可运行的拓扑——每个
  示例只展示一项特性，比这个「大杂烩」教程更易读。
- [API 参考](api/network.md)记录了上文用到的每一个类与函数。
- [路线图](roadmap.md)列出 v0.3.0 候选项，包括 PSA 架构支持
  与异步 P4Runtime 客户端。
