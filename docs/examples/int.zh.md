---
description: 单交换机 INT（带内网络遥测）示例。交换机在每个转发包里插入 14 字节 shim；接收端用原始套接字解码。
---

# INT（带内网络遥测）

单交换机示例：P4 流水线给每个转发的 IPv4 包嵌入一个 14 字节的 INT shim 头。shim 中携带交换机 ID、入端时间戳、出端口、队列深度，以及原始 etherType。接收主机命名空间里的原始套接字 listener 解析 shim，按包打印结构化遥测。

## 跑起来

在第一个终端中，从仓库根目录启动拓扑。

```sh
sudo p4net examples/int/topology.py
```

出现 `p4net>` 提示符后，打开第二个终端，在仓库根目录启动 `h2` 中的监听器。

```sh
sudo ip netns exec h2 python3 examples/int/listener.py --iface h2-eth0 --count 3
```

看到监听器输出 `waiting for INT frames` 后，在第三个终端发送三次 ICMP 请求。

```sh
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

交换机给每个请求加上一个 INT 头。监听器能收到三个帧。接收主机没有返回 ICMP 回复，所以 `ping` 显示 `100% packet loss`。

监听器为每个帧打印一行。

```
[listener] bound on h2-eth0, waiting for INT frames
[switch=1 ts=745907us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=1750021us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=2754336us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
```

监听器显示三个帧后自动退出。然后在 `p4net>` 提示符下输入 `exit` 停止拓扑。

## 拓扑

`examples/int/topology.py`：

```python
--8<-- "examples/int/topology.py"
```

`setup(net)` 通过 P4Runtime 下发 IPv4 转发表项，并为两台主机配置静态 ARP。

## P4 程序

`examples/int/int.p4`：

```p4
--8<-- "examples/int/int.p4"
```

<span id="这个示例展示了什么"></span>要点：

- shim 有效时，deparser 将它插入以太网头与 IPv4 头之间。外层 EtherType 是 `0x88B6`，shim 的 `next_proto` 保存原始 EtherType（IPv4 为 `0x0800`）。
- ingress 控制在 LPM 表设好 `std.egress_spec` 之后再从 `standard_metadata` 填 shim。
- `switch_id` 现在是寄存器（``register<bit<8>>(1) switch_id;``）。拓扑的 ``setup(net)`` 用 ``s1.client.write_register("MyIngress.switch_id", index=0, value=1)`` 写入；多交换机 INT 部署可以无重编译地为每台分配独立编号。

## listener

`examples/int/listener.py`：

```python
--8<-- "examples/int/listener.py"
```

listener 打开原始 `AF_PACKET` 套接字，按 `etherType == 0x88B6` 过滤，按字节偏移解 14 字节 shim，打印结构化结果。

## 注意事项

- **`queue_depth` 几乎总是 0**：BMv2 默认队列下，除非出端队列确实积压（本示例流量根本不会），该字段就停在 0。线路通了仅此而已。
- [两台交换机的 INT 示例](int-multi-hop.md)展示了如何在每一跳加一个 shim。
- **交换机 ID 由寄存器配置**：改拓扑 ``setup(net)`` 中 ``write_register("MyIngress.switch_id", index=0, value=N)`` 的 ``N`` 即可换标。多交换机部署给每台分配不同 ``N`` ，无须重编译。

## 可以试试

- 加第二个交换机，把它的 ``switch_id`` 寄存器写为 ``2``，串成 h1 → s1 → s2 → h2，扩展 listener（或 P4 流水线）处理 shim 栈。
- 把 listener 输出重定向到文件，离线算出基于 `ingress_timestamp_us` 的逐流延迟差。
- 给某条 h↔s 链路加 `delay="50ms"` 或 `loss_pct=2.0`，看时间戳与包数是否如预期变化。
