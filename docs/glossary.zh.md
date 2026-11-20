---
description: p4net 文档中使用到的 P4、SDN、Linux 网络与工具相关术语的中英文对照表。
---

# 术语表

## P4 生态

`P4_16`
:   P4Net 示例采用的 P4 语言版本。

`v1model`
:   BMv2 的 `simple_switch` 和 `simple_switch_grpc` 使用的 P4 架构。它定义了解析、入口处理、流量管理、出口处理和报文重组等阶段。

`PSA`
:   可移植交换机架构（Portable Switch Architecture）。它定义了可编程交换机的报文处理模块和常用 extern。p4net 目前只支持 `v1model`。参见 [P4 架构支持](known-limitations.md#3-不支持-psa-架构)。

`p4c`
:   P4 编译器。P4Net 用它生成 BMv2 JSON 和 P4Info。

`P4Info`
:   描述编译后 P4 程序中表、动作、计数器、寄存器以及控制器报头布局的 protobuf 消息。让通用 P4Runtime 客户端能够按名字定位具体流水线。

`BMv2`
:   Behavioral Model 第 2 版，P4 的参考软件交换机。`simple_switch_grpc` 可运行 v1model 程序并提供 P4Runtime 服务。

`simple_switch_grpc`
:   P4Net 启动的 BMv2 可执行程序。第一台交换机默认在 50051 端口提供 P4Runtime gRPC 服务，在 9090 端口提供 Thrift 服务。两类端口都可按交换机设置。

`P4Runtime`
:   通过 gRPC 控制 P4 设备的协议，支持配置流水线、表项、计数器、寄存器和组播组，也可通过 StreamChannel 收发报文。

## P4 数据平面原语

`Match-Action Table`
:   `(匹配键) → (动作, 参数)` 的表。数据包字段值匹配某条表项；该表项的动作携带预设参数运行。

`LPM`
:   Longest Prefix Match。IP 路由表常用的匹配键类型；表项是 `(prefix, prefix_length)`。

`Ternary`
:   带掩码的「取值-掩码」匹配（任意位都可以「不关心」）。需要 `priority` 字段消除歧义。

`Range`
:   值在 `[low, high]` 区间内的匹配键。需要 `priority`。

`Exact`
:   精确相等的匹配键。

`Action`
:   表项命中时执行的 P4 例程；修改报头、设置出口端口等。可以带参数（`set_egress_port(bit<9> port)`）。

`Counter`
:   P4 侧的包/字节计数器。直接计数器绑定到表；间接计数器是独立数组，按 32 位索引访问。

## 控制平面概念

`Control plane`
:   配置和监控转发行为的软件。在 P4Net 中，可以通过 Python 控制器或命令行下发表项、读取计数器和接收 PacketIn。

`Data plane`
:   交换机中按已加载的程序处理报文的部分。P4Net 在 BMv2 中运行该程序。

`Pipeline`
:   交换机当前运行的编译后数据平面程序（parser + 控制 + deparser）。通过 P4Runtime 的 `SetForwardingPipelineConfig` 推送。

`Election ID`（选举 ID）
:   控制器在 P4Runtime 仲裁时提交的 128 位无符号整数。同一设备和角色下，ID 较大的控制器优先。`Network` 创建的客户端和未显式指定 ID 的 `AsyncP4RuntimeClient` 都用 Unix 毫秒时间戳作为高 64 位，低 64 位为 0。同一毫秒生成的 ID 可能重复。需要显式设置时，参见[连接与主控权](async-client.md#连接与主控权)。

`Mastership`（主控权）
:   控制器对某个设备和角色的主控权。P4Runtime 对每个 `(device_id, role)` 组合单独选主，同一组合最多有一个主控客户端。主控可以在该角色的权限范围内写入。参见 [P4Runtime 客户端仲裁规范](https://p4lang.github.io/p4runtime/spec/v1.5.0/P4Runtime-Spec.html#sec-client-arbitration-and-controller-replication)。

## Linux 网络

`Network namespace`（`netns`）
:   拥有独立接口、地址、路由表和邻居表的网络栈。P4Net 为每台主机创建单独的网络命名空间。

`veth pair`
:   一对相连的虚拟以太网接口。报文从一端发出后会到达另一端。P4Net 用它连接拓扑中的节点。

`qdisc`
:   队列调度规则（Queueing discipline）。它决定 Linux 接口如何对报文排队、调度或丢弃。

`netem`
:   用于网络仿真的 Linux 队列调度规则，可模拟时延、抖动、丢包、乱序和报文损坏。

`tc`
:   配置 Linux 流量控制的命令，包括配置 `netem` 等队列调度规则。

`sysctl`
:   读取和修改 `/proc/sys` 下内核参数的命令。P4Net 用 `net.ipv6.conf.<iface>.disable_ipv6` 为单个接口启用或关闭 IPv6。

`Link-local address`
:   仅在本链路使用的 IPv6 地址，属于 `fe80::/10`。接口启用 IPv6 后，Linux 根据 `addr_gen_mode` 设置决定是否自动生成此类地址。

`SLAAC`
:   无状态地址自动配置（Stateless Address Autoconfiguration）。p4net 在启用 IPv6 的主机接口上设置 `accept_ra=0` 和 `autoconf=0`，禁止根据路由器通告自动配置地址。

`ND`（Neighbor Discovery）
:   IPv6 的邻居发现协议，可发现路由器和邻居，也可解析 IPv6 地址对应的链路层地址。

`ARP`
:   地址解析协议（Address Resolution Protocol）。它用于查询本地链路上 IPv4 地址对应的 MAC 地址。[L3 转发示例](examples/l3-forwarding.md)为 IPv4 转发程序配置了静态 ARP 条目。

## 工具与打包

`PyPI`
:   Python 软件包索引（Python Package Index）。P4Net 的发布包名为 `p4net`。

`Graphviz DOT`
:   `Topology.to_graphviz()` 输出的图描述语言。`dot` 命令把 DOT 渲染为 PNG/SVG/PDF。
