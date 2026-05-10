---
description: p4net 文档中使用到的 P4、SDN、Linux 网络与工具相关术语的中英文对照表。
---

# 术语表

## P4 生态

`P4_16`
:   P4 语言（数据平面编程语言）当前的主要版本。p4net 只消费
    P4_16 源码。

`v1model`
:   BMv2 `simple_switch` / `simple_switch_grpc` 使用的 P4 参考
    架构。规定了 p4net 假设的 parser → ingress → traffic-manager
    → egress → deparser 形态。

`PSA`
:   Portable Switch Architecture。比 v1model 更强的后继架构。
    p4net 在 v0.x 不针对 PSA；见[路线图](roadmap.md)。

`p4c`
:   官方 P4 编译器。p4net 调用 `p4c -b bmv2
    --p4runtime-files=p4info.txtpb` 同时产出 BMv2 JSON 与
    P4Info。

`P4Info`
:   描述编译后 P4 程序中表、动作、计数器、寄存器以及控制器
    报头布局的 protobuf 消息。让通用 P4Runtime 客户端能够按
    名字定位具体流水线。

`BMv2`
:   Behavioral Model 第 2 版——官方软件 P4 数据平面。
    `simple_switch_grpc` 是开启 P4Runtime 服务的 v1model BMv2
    二进制。

`simple_switch_grpc`
:   p4net 实际使用的 BMv2 二进制。监听可配置的 gRPC 端口
    （默认 50051）提供 P4Runtime，可选监听 Thrift 端口
    （默认 9090）提供数据平面调试通道。

`P4Runtime`
:   P4 数据平面的标准控制平面协议。通过 protobuf 定义；运行在
    gRPC 之上。覆盖流水线配置、表项 CRUD、计数器读取、寄存器
    读取、组播组管理，以及用于异步数据包 I/O 的 StreamChannel。

## P4 数据平面原语

`Match-Action Table`
:   `(匹配键) → (动作, 参数)` 的表。数据包字段值匹配某条表项；
    该表项的动作携带预设参数运行。

`LPM`
:   Longest Prefix Match。IP 路由表常用的匹配键类型；表项是
    `(prefix, prefix_length)`。

`Ternary`
:   带掩码的「取值-掩码」匹配（任意位都可以「不关心」）。需要
    `priority` 字段消除歧义。

`Range`
:   值在 `[low, high]` 区间内的匹配键。需要 `priority`。

`Exact`
:   精确相等的匹配键。

`Action`
:   表项命中时执行的 P4 例程；修改报头、设置出口端口等。可以
    带参数（`set_egress_port(bit<9> port)`）。

`Counter`
:   P4 侧的包/字节计数器。直接计数器绑定到表；间接计数器是独立
    数组，按 32 位索引访问。

## 控制平面概念

`Control plane`
:   决定数据平面应该做什么的组件——下发表项、读计数器、响应
    packet-in。对 p4net 而言，控制平面就是你的 Python 拓扑加
    setup 代码。

`Data plane`
:   按线速处理单个数据包的组件。对 p4net 而言是
    `simple_switch_grpc`。

`Pipeline`
:   交换机当前运行的编译后数据平面程序（parser + 控制 +
    deparser）。通过 P4Runtime 的
    `SetForwardingPipelineConfig` 推送。

`Election ID`
:   一个 128 位整数，用于在多个 P4Runtime 控制器竞争主控权时
    排序。最大者胜出。p4net 使用毫秒级时间戳，使重跑同一脚本
    总能稳定地夺得主控者地位。

`Mastership`
:   设备当前的主控者。P4Runtime 一次只允许一个主控写入者。

## Linux 网络

`Network namespace`（`netns`）
:   内核隔离的网络栈：独立的接口、路由表、IP 地址、邻居缓存。
    p4net 把每台主机放进各自的命名空间。

`veth pair`
:   一对相互连接的虚拟以太网接口——从其一发出的包出现在另一端。
    p4net 用它把主机（在自己的命名空间里）和交换机（在 root
    命名空间）连起来。

`qdisc`
:   Queueing discipline。Linux 在接口上的流量控制状态机；负责
    调度、整形、丢包。

`netem`
:   网络仿真 `qdisc`——加可配置的延迟、抖动、丢包、乱序、损坏。

`tc`
:   编程 `qdisc` 的用户态工具。p4net 通过
    `p4net.runtime.apply_netem` 调用
    `tc qdisc add ... root netem ...`。

`sysctl`
:   读写内核 `/proc/sys` 旋钮的用户态工具。p4net 用
    `sysctl -w net.ipv6.conf.<iface>.disable_ipv6=...` 实现
    按接口的 IPv6 门控。

`Link-local address`
:   接口启用 IPv6 时自动生成的不可路由 IPv6 地址（`fe80::/10`）。
    p4net 在未显式请求 IPv6 的接口上抑制此自动行为。

`SLAAC`
:   StateLess Address AutoConfiguration——IPv6 主机基于路由
    通告自动派生地址。p4net 默认关闭（`accept_ra=0`、
    `autoconf=0`），仅保留显式分配的地址。

`ND`（Neighbor Discovery）
:   IPv6 的邻居发现，对应 IPv4 的 ARP；解析 IPv6 到 MAC。

`ARP`
:   Address Resolution Protocol。把 IPv4 解析为 MAC。p4net 的
    示例都注入静态 ARP，因为 BMv2 没有内置 ARP 响应器。

## 工具与打包

`PyPI`
:   Python Package Index。p4net 在此发布 wheel，包名为 `p4net`。

`MkDocs`
:   p4net 文档站点使用的静态站点生成器。

`Material for MkDocs`
:   本站使用的 MkDocs 主题，提供搜索、深色模式、社交卡片、
    导航元素等能力。

`mkdocstrings`
:   从 Python docstring 自动生成 API 参考的 MkDocs 插件。

`Graphviz DOT`
:   `Topology.to_graphviz()` 输出的图描述语言。`dot` 命令把
    DOT 渲染为 PNG/SVG/PDF。
