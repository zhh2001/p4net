---
description: 两台主机的链路按方向整形。演示 `delay_a_to_b` / `delay_b_to_a` 以及由此产生的非对称单向 RTT。
---

# 非对称链路

一台交换机上的两台主机，每条链路只在单一方向上加延迟，反向不
整形。最终的 ping RTT 等于两条单向延迟之和。

## 你将看到什么

`h1 ping h2` 报告大约 220ms 的 RTT，抖动在亚毫秒级别——集成测试
中实测的 RTT 是
`min/avg/max/mdev = 220.981/221.288/222.048/0.396 ms`。

## 拓扑

`examples/asymmetric_link/topology.py`：

```python
--8<-- "examples/asymmetric_link/topology.py"
```

h1↔s1 链路上的 `delay_a_to_b="200ms"` 应用在 a 侧（`h1` 命名
空间）。h2↔s1 链路上的 `delay_b_to_a="20ms"` 应用在 b 侧
（root 命名空间里 s1 朝向 h2 的 veth）。

| 方向              | 路径                  | 整形延迟 |
| ----------------- | --------------------- | -------- |
| h1 → s1           | h1 出向 veth          | 200ms    |
| s1 → h2           | s1 出向 veth          | 20ms     |
| h2 → s1           | （无）                | 0ms      |
| s1 → h1           | （无）                | 0ms      |

端到端单向 h1→h2：200 + 20 = 220ms。反向 h2→h1：0ms。
ping RTT（h1→h2 echo + h2→h1 reply）：220ms。

## P4 程序

`examples/asymmetric_link/asymmetric.p4`：

```p4
--8<-- "examples/asymmetric_link/asymmetric.p4"
```

与[快速上手](quick-start.md)相同的端口翻转流水线。非对称完全
依赖链路损伤实现，与数据平面无关。

## 运行

```
sudo p4net examples/asymmetric_link/topology.py
```

```
p4net> h1 ping h2 5 3
PING 10.0.0.2 (10.0.0.2) 56(84) bytes of data.
64 bytes from 10.0.0.2: icmp_seq=1 ttl=64 time=222 ms
64 bytes from 10.0.0.2: icmp_seq=2 ttl=64 time=221 ms
64 bytes from 10.0.0.2: icmp_seq=3 ttl=64 time=221 ms
64 bytes from 10.0.0.2: icmp_seq=4 ttl=64 time=221 ms
64 bytes from 10.0.0.2: icmp_seq=5 ttl=64 time=221 ms

--- 10.0.0.2 ping statistics ---
5 packets transmitted, 5 received, 0% packet loss, time 4126ms
rtt min/avg/max/mdev = 220.981/221.288/222.048/0.396 ms
```

（这是 phase-12 验证时实测的输出。）

在 h1 命名空间执行 `tc qdisc show` 可以确认 netem 队列在哪一侧：

```
p4net> h1 cmd tc qdisc show dev h1-eth0
qdisc netem 8001: root ... delay 200ms
```

## 关键设计点

- **方向语义由 `(a, b)` 顺序决定**。
  `Link(h1, s1, delay_a_to_b="200ms")` 整形 h1→s1。如果交换写法
  为 `Link(s1, h1, ...)`，同样的 `delay_a_to_b` 整形的将是
  s1→h1。`add_link(a, b, ...)` 的参数顺序就是基准。
- **同一参数同时给对称值与非对称值会被构造期拒绝**。不能既写
  `delay="50ms"` 又写 `delay_a_to_b="100ms"`——同一个参数只能
  二选一。但跨参数自由组合是允许的（例如对称 `bandwidth` 加
  非对称 `delay`）。

## 可尝试的变体

- 改用 `loss_pct_a_to_b=50.0` 替代延迟，观察 `pingall` 矩阵呈
  50% 丢包。
- 把 `delay_a_to_b="200ms"` 与 `delay_b_to_a="200ms"` 同时设上，
  恢复对称 400ms RTT。
- 用非对称延迟搭配对称带宽整形（`bandwidth="1mbit"`）模拟典型的
  家用宽带链路。
