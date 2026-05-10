---
description: 把每个数据平面包都通过 CPU 端口上送控制器的流水线。演示 `<switch> packet send` 与 `<switch> packet listen`。
---

# CPU 上送

一台主机、一台交换机，每个数据平面包都被通过 CPU 端口（510）
上送控制器。控制器也可以通过带有显式出口端口元数据的
`PacketOut` 把数据包注入回数据平面。

## 你将看到什么

主机产生 ARP / IPv6 ND / ICMP 流量时，`s1 packet listen` 会实时
显示上送上来的数据包。`s1 packet send` 把控制器构造的帧反向
注入数据平面。

## 拓扑

`examples/cpu_punt/topology.py`：

```python
--8<-- "examples/cpu_punt/topology.py"
```

交换机上的 `cpu_port=510` 是把 CPU 端口接入 BMv2 的关键参数。

## P4 程序

`examples/cpu_punt/cpu_punt.p4`：

```p4
--8<-- "examples/cpu_punt/cpu_punt.p4"
```

两个 `@controller_header` 声明分别定义 PacketIn（上送控制器）与
PacketOut（控制器注入）的元数据布局。parser 根据
`std.ingress_port == CPU_PORT` 判断是否需要在 ethernet 之前先解析
`packet_out` 报头。入口控制：

- 控制器注入的包：把 `egress_port` 复制进 `std.egress_spec`，
  并使控制器报头无效。
- 数据平面包：把 `std.egress_spec = CPU_PORT`，使
  `packet_in` 报头有效，并写入 `ingress_port`。

## 运行

```
sudo p4net examples/cpu_punt/topology.py
```

Shell 中：

```
p4net> s1 packet listen count=3 timeout=5
[ingress_port=1] 333300000016000000000001...
[ingress_port=1] 333300000016000000000001...
[ingress_port=1] ff02000000000000000000010002...
```

`[ingress_port=1]` 这个前缀是被解码出来的 `packet_in` 控制器报头。
hex 负载在 CLI 中截断到 64 字符；如需完整 payload，在 Python 端
使用 `client.expect_packet_in()`。

从控制器注入一个朝向 `h1` 的帧：

```
p4net> s1 packet send ffffffffffff000000000001880b48656c6c6f \
         metadata: egress_port=1
ok
```

## 关键设计点

- **BPF 过滤器小技巧**。集成测试要验证控制器注入的帧确实到达
  `h1` 时，使用的是 `tcpdump -i h1-eth0 -c 1 ether proto 0x88B5`，
  而非裸 `tcpdump -c 1`。无过滤的话，IPv6 ND 噪声会先把
  count-1 的位置占掉，测试帧到来时 tcpdump 已经退出。本示例使用
  `0x88B`（local-experimental 以太网类型）就是同样的考虑。
- **缺失的元数据自动补零**。`encode_packet_out_metadata` 遍历
  P4Info 中声明的每一个 metadata 字段，对未给出的键回退到
  `metadata.get(name, 0)`，因此 `_pad0` 调用方无需显式指定。

## 可尝试的变体

- 用 `s1.client.on_packet_in(handler)` 注册一个 Python 处理器，
  解析上送的以太网帧，做学习交换机式的逻辑。
- 在 setup 脚本里用
  `s1.client.send_packet_out(payload, {"egress_port": 1})`
  注入一串探测帧。
- 加一张 `default_action = punt()` 的 `ipv4_lpm` 表，把已编程的
  转发与控制器兜底逻辑混合起来。
