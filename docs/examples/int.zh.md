---
description: 单交换机 INT（带内网络遥测）示例。交换机在每个转发包里插入 14 字节 shim；接收端用原始套接字解码。
---

# INT（带内遥测）

单交换机示例：P4 流水线给每个转发的 IPv4 包嵌入一个 14 字节的
INT（带内网络遥测）shim 头。shim 中携带交换机 ID、入端时间戳、
出端口、队列深度，以及原始 etherType。接收主机命名空间里的原始
套接字 listener 解析 shim，按包打印结构化遥测。

## 这个示例展示了什么

- **线缆级头插入**：P4 deparser 在以太网与 IPv4 之间发出一个新头。
- **EtherType 替换**：外层 etherType 改成 `0x88B6`（INT shim 标识），
  这样内核与抓包过滤器能分清 INT 帧。
- **原始 etherType 保留**：shim 的 `next_proto` 字段保留原始
  etherType（IPv4 是 `0x0800`），接收端可以恢复内层头链。
- **原始套接字解码**：用户态 listener 通过 `AF_PACKET` 收帧，按
  字节偏移解析 shim，每帧打一行。

## 拓扑

`examples/int/topology.py`：

```python
--8<-- "examples/int/topology.py"
```

两主机一交换机，P4Runtime 跑 IPv4 转发，加静态 ARP。

## P4 程序

`examples/int/int.p4`：

```p4
--8<-- "examples/int/int.p4"
```

要点：

- shim 头静态声明，deparser 按 valid 位条件发送。
- ingress 控制在 LPM 表设好 `std.egress_spec` 之后再从
  `standard_metadata` 填 shim。
- `switch_id` 用 `const` 写死，因为当前 P4Runtime 客户端没有
  寄存器写 API；要做每交换机参数化，模式是用默认动作表或者按
  交换机重编译。

## listener

`examples/int/listener.py`：

```python
--8<-- "examples/int/listener.py"
```

listener 打开原始 `AF_PACKET` 套接字，按 `etherType == 0x88B6`
过滤，按字节偏移解 14 字节 shim，打印结构化结果。

## 跑起来

一个终端：

```
sudo p4net examples/int/topology.py
```

`setup(net)` 装 LPM 条目，预置静态 ARP，落到 `p4net>` shell。

另一个终端（或者从 shell 用 `h2 xterm` 起）：

```
sudo ip netns exec h2 python3 examples/int/listener.py --iface h2-eth0
```

再一个终端发流量：

```
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

每过交换机一个包，listener 打一行：

```
[listener] bound on h2-eth0, waiting for INT frames
[switch=1 ts=164832000ns egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=165834200ns egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=166836100ns egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
```

## 注意事项

- **`queue_depth` 几乎总是 0**：BMv2 默认队列下，除非出端队列
  确实积压（本示例流量根本不会），该字段就停在 0。线路通了
  仅此而已。
- **只演示单跳**：真实 INT 每过一跳叠一层 shim；多跳留作扩展
  练习。
- **交换机 ID 写死**：改 `int.p4` 里的 `SWITCH_ID`。多交换机部署
  的常规做法是用一行默认动作表，每台交换机各自填值。

## 可以试试

- 加第二个交换机 `SWITCH_ID = 2`，串成 h1 → s1 → s2 → h2，
  扩展 listener（或 P4 流水线）处理 shim 栈。
- 把 listener 输出重定向到文件，离线算出基于
  `ingress_timestamp_ns` 的逐流延迟差。
- 给某条 h↔s 链路加 `delay="50ms"` 或 `loss_pct=2.0`，看
  时间戳与包数是否如预期变化。
