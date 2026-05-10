---
description: 两台主机由 ipv4_lpm 表转发，表项在启动时由 Python 编程下发。演示运行时表项编程。
---

# L3 转发

一台交换机上的两台主机，数据平面通过启动时由 Python 编程下发的
`ipv4_lpm` 表来转发。

## 你将看到什么

`pingall` 成功，因为控制器在 Shell 进入之前为两台主机各装了一条
`/32` 路由。

## 拓扑

`examples/l3_forwarding/topology.py`：

```python
--8<-- "examples/l3_forwarding/topology.py"
```

`setup(net)` 调用了两次 `client.insert_table_entry(...)`——每台
主机一次——表名使用 P4Info 中的全限定名。

## P4 程序

`examples/l3_forwarding/ipv4_lpm.p4`：

```p4
--8<-- "examples/l3_forwarding/ipv4_lpm.p4"
```

入口控制只在存在 IPv4 报头时应用 `ipv4_lpm`——非 IPv4 流量
（例如 ARP）走默认 `NoAction`，无处可去。ARP 之所以能工作，是
因为 `setup(net)` 注入了静态条目。

## 运行

```
sudo p4net examples/l3_forwarding/topology.py
```

Shell 中：

```
p4net> s1 table dump MyIngress.ipv4_lpm
#0
  table:    MyIngress.ipv4_lpm
  match:    {'hdr.ipv4.dstAddr': '10.0.0.1/32'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
#1
  table:    MyIngress.ipv4_lpm
  match:    {'hdr.ipv4.dstAddr': '10.0.0.2/32'}
  action:   MyIngress.set_egress_port
  params:   {'port': '2'}

p4net> pingall
H \ H   h1   h2
   h1    -    1
   h2    1    -
2/2 succeeded

p4net> s1 counter MyIngress.ingress_pkts 1
pkts=1 bytes=98
```

match 值渲染为 `10.0.0.1/32`——这是 `decode_match` 把 P4Runtime
规范字节还原为人类可读的 IPv4 字符串。

## 关键设计点

- 同一份数据平面可以承载 5 主机、100 主机或完全不同的 L3 设计——
  改变的只是表项编程，不动 P4。
- `s1.client.insert_table_entry(...)` 接受普通 Python 类型
  （字符串、字典、整数）；`P4InfoIndex` 根据已加载的 P4Info 把
  它们翻译成 P4Runtime 的 FieldMatch 与 Action proto。

## 可尝试的变体

- 加一台 `10.0.0.3/24` 的主机，再装一条 LPM 表项。无需修改 P4。
- 把两条 `/32` 替换为同一条覆盖整个子网、走相同出口的 `/24`，
  验证 `pingall` 仍然成功。
- 增加一台位于 `10.1.0.0/24` 的主机，使用 `MyIngress.drop`
  动作丢弃其流量——观察 LPM 从最长前缀向最短前缀解析的过程。
