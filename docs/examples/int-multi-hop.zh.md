---
description: 多跳 INT 示例。两台交换机串联运行同一份 P4，每台插入自己的元数据 shim；接收端解析整条逐跳链。
---

# 多跳 INT（带内遥测）

两台交换机串联，每台在每个转发包里插入自己的 14 字节 INT shim 头。
接收端解析整条逐跳栈，重建包穿过拓扑的轨迹。这是更贴近真实部署
形态的 INT 示例；想看更简单的单交换机入门，请参见
[INT（带内遥测）](int.md)。

## 这个示例展示了什么

- **逐跳元数据累积**：路径上每台交换机都插入自己的 shim，出端
  接收方按交换机数量看到对应块的元数据。
- **`next_proto` 链式拼接**：每个 shim 的 `next_proto` 字段指明顺序
  上的下一个头。解析器走
  `etherType → shim_1.next_proto → shim_2.next_proto → ipv4`，
  两跳情况下无须 P4 header stack。
- **来自寄存器的逐交换机身份**：同一份 P4 程序在两台交换机上运行；
  每台启动时通过 v1.2 寄存器 API
  `write_register("MyIngress.switch_id_reg", index=0, value=N)`
  写入自己的 `switch_id`。

## 拓扑

`examples/int_multi_hop/topology.py`：

```python
--8<-- "examples/int_multi_hop/topology.py"
```

四个节点、三条链路，线性路径：`h1 — s1 — s2 — h2`。

## P4 程序

`examples/int_multi_hop/int_multi_hop.p4`：

```p4
--8<-- "examples/int_multi_hop/int_multi_hop.p4"
```

要点：

- 用两个命名 header 实例 `int_shim_1`、`int_shim_2`，不用 P4
  header stack。两跳情况下更易读；要做 N 跳，请参考示例 README 的
  「扩展到 N 跳」一节。
- ingress 选择第一个未填的 shim slot，从 `standard_metadata` 与配置
  的 `switch_id` 写入。`next_proto` 链路被重新拼接，使接收方看到
  `eth → shim_1 → shim_2 → ipv4` 的顺序。
- deparser 按声明顺序 emit 所有 valid header。

## listener

`examples/int_multi_hop/listener.py`：

```python
--8<-- "examples/int_multi_hop/listener.py"
```

listener 从外层 EtherType 开始遍历 shim 链，每跳解出一个 14 字节
shim，直到 `next_proto` 离开 INT 范围为止。

## 跑起来

一个终端：

```
sudo p4net examples/int_multi_hop/topology.py
```

`setup(net)` 给两台交换机装 L2 转发表、给两台主机预置静态 ARP，并
写入各自的身份寄存器。落到 `p4net>` shell。

另一个终端（或从 shell 起 `h2 xterm`）：

```
sudo ip netns exec h2 python3 examples/int_multi_hop/listener.py --iface h2-eth0
```

再开一个终端：

```
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

listener 每过一个包打一个块；本拓扑中每个块两行（两个交换机）。

## 示例输出

来自多跳集成测试的实测：

```
packet (2 hop(s), final proto 0x0800): 10.0.0.1 -> 10.0.0.2
  hop 1: switch_id=1 ts=874083us egress_port=2 queue_depth=0
  hop 2: switch_id=2 ts=769824us egress_port=2 queue_depth=0
packet (2 hop(s), final proto 0x0800): 10.0.0.1 -> 10.0.0.2
  hop 1: switch_id=1 ts=1875211us egress_port=2 queue_depth=0
  hop 2: switch_id=2 ts=1770811us egress_port=2 queue_depth=0
```

`hop 1` 是 s1（出口 port 2 朝向 s2）；`hop 2` 是 s2（出口 port 2
朝向 h2）。listener 按插入顺序打印 hop，包最先进入的交换机始终
显示为 `hop 1`。

## 值得注意的点

- **时间戳是每台交换机进程的本地时间**。BMv2 的
  `standard_metadata.ingress_global_timestamp` 在每个
  `simple_switch_grpc` 进程启动时从零开始。两个 shim 的时间戳不能
  直接相减——差值反映的是两个独立进程之间的启动偏移，而不是线路
  延迟。要做真实的每条链路延迟，需要共享时间源（PTP 风格）或者
  由控制器记录每台交换机的启动偏移。
- **出端口对应路径方向**。s1 从 port 2 朝 s2 转发；s2 从 port 2
  朝 h2 转发。不同拓扑会得到不同的端口号。
- **`queue_depth` 在本负载下稳定为 0**——BMv2 的默认队列设置下，
  没有显式队列配置和饱和负载是看不到非零值的。

## 注意事项

- **当前流水线只支持两跳**。第三台交换机会发现两个 shim slot 都已
  valid，直接转发不再追加。真实部署用 MAX_HOPS 深度的 P4 header
  stack——示例 README 里有改写步骤。
- **`queue_depth` 几乎总是 0**，与单交换机示例一致。
- **不重算插入 shim 的校验和**。IPv4 校验和只覆盖 IPv4 头本身；
  位于以太网与 IPv4 之间的 shim 层是无保护的，这与 INT 规范的
  假设（链路层完整性）一致。
