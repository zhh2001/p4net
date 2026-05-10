---
description: 两台仅 IPv6 的主机，128 位 LPM 表在运行时编程。展示 `<switch> table dump` 中的 IPv6 codec 往返。
---

# IPv6 LPM

一台交换机上的两台仅 IPv6 主机，使用 `ipv6_lpm` 表按 128 位
目的地址匹配。表项在运行时通过 P4Runtime 编程下发。本例的看点是
`<switch> table dump` 把 IPv6 表项渲染为人类可读形式（`fd00::1/128`）
而非裸字节。

## 你将看到什么

`pingall6` 成功；`s1 table dump MyIngress.ipv6_lpm` 显示
`fd00::1/128` 与 `fd00::2/128`；每次 ping 后按端口的计数器都会
增加。

## 拓扑

`examples/ipv6_lpm/topology.py`：

```python
--8<-- "examples/ipv6_lpm/topology.py"
```

`Host.ip6` 是唯一的 L3 地址——两台主机都仅 IPv6。编排器在拉起
接口之前调用 `enable_ipv6(ns, iface)` 并赋上 `fd00::1/64` /
`fd00::2/64`。

## P4 程序

`examples/ipv6_lpm/ipv6_lpm.p4`：

```p4
--8<-- "examples/ipv6_lpm/ipv6_lpm.p4"
```

两个值得注意的点：

- 匹配键是 `bit<128> dstAddr`，类型 `lpm`——运行时层存储的是
  规范字节加上前缀长度，`decode_match` 知道把 128 位字段渲染为
  IPv6。
- `set_egress_port` 累加一个间接计数器，方便我们从控制器端
  验证转发是否生效。

## 运行

```
sudo p4net examples/ipv6_lpm/topology.py
```

```
p4net> hosts
name  primary_ip  primary_ip6  interfaces
h1    -           fd00::1/64   h1-eth0
h2    -           fd00::2/64   h2-eth0

p4net> s1 table dump MyIngress.ipv6_lpm
#0
  table:    MyIngress.ipv6_lpm
  match:    {'hdr.ipv6.dstAddr': 'fd00::1/128'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
#1
  table:    MyIngress.ipv6_lpm
  match:    {'hdr.ipv6.dstAddr': 'fd00::2/128'}
  action:   MyIngress.set_egress_port
  params:   {'port': '2'}

p4net> pingall6
H \ H   h1   h2
   h1    -    1
   h2    1    -
2/2 succeeded

p4net> s1 counter MyIngress.ipv6_pkts 2
pkts=1 bytes=118
```

（上面的 table dump 输出来自 phase-13 集成测试的实际捕获。）

## 关键设计点

- **位宽感知的解码会为 128 位字段选择 IPv6 格式**。同一个
  `decode_match` 把 32 位字段渲染为 IPv4，48 位字段渲染为 MAC，
  128 位字段渲染为 IPv6 紧凑形式——P4Info 中无需任何按字段的
  注解，位宽就是线索。
- **规范字节的往返干净**。`encode_value("fd00::1", 128)` 产出
  `b'\xfd\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01'`。
  P4Runtime 通过去掉前导零来规范化，BMv2 存储控制器实际发出的
  字节。读回时 `decode_ipv6` 在高位（最高位字节）一侧补零回到
  16 字节，再交给 `ipaddress.IPv6Address.__str__` 渲染。
  phase-13 集成测试已验证。

## 可尝试的变体

- 把一条 `/128` 替换为 `/64`（覆盖两台主机），同时再装一条
  `/128`，观察 LPM 在两条都安装时如何按更长前缀解析。
- 加一台 `fd00::3/64` 的主机，运行时（无需修改 P4，无需重启）
  从 Python 编程注入路由。
- 在 Python 控制器中用 `client.read_counter("MyIngress.ipv6_pkts")`
  周期性轮询计数器，观察流量。
