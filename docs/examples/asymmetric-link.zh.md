---
description: 两台主机之间的单向时延不同。演示 `delay_a_to_b` 和 `delay_b_to_a` 如何影响 ping 的往返时延。
---

# 非对称链路

两台主机通过一台交换机连接，每段链路只在一个方向上增加时延。

## 运行

在仓库根目录启动拓扑。

```sh
sudo p4net examples/asymmetric_link/topology.py
```

`h1 ping h2 5 3` 和 `h2 ping h1 5 3` 显示的往返时延（RTT）都约为 220 ms。

```
p4net> h1 ping h2 5 3
PING h2 (10.0.0.2)
seq=1  time=222 ms
seq=2  time=221 ms
seq=3  time=221 ms
seq=4  time=221 ms
seq=5  time=221 ms
5 sent, 5 received, 0% loss
```

在 h1 命名空间执行 `tc qdisc show` 可以确认 netem 队列在哪一侧：

```
p4net> h1 cmd tc qdisc show dev h1-eth0
qdisc netem 8001: root ... delay 200ms
```

完成后，在 `p4net>` 提示符下输入 `exit` 停止拓扑。

## 拓扑

`examples/asymmetric_link/topology.py`：

```python
--8<-- "examples/asymmetric_link/topology.py"
```

`h1` 和 `s1` 之间的 `delay_a_to_b="200ms"` 作用于 `h1` 的出口。`h2` 和 `s1` 之间的 `delay_b_to_a="20ms"` 作用于 `s1` 朝向 `h2` 的出口。

| 方向    | 附加时延 |
| ------- | -------- |
| h1 → s1 | 200 ms   |
| s1 → h2 | 20 ms    |
| h2 → s1 | 0 ms     |
| s1 → h1 | 0 ms     |

从 `h1` 到 `h2` 共增加 220 ms 时延，反向没有配置额外时延。无论从哪台主机发起 ping，请求和应答都分别经过这两个方向。

## P4 程序

`examples/asymmetric_link/asymmetric.p4`：

```p4
--8<-- "examples/asymmetric_link/asymmetric.p4"
```

与[快速上手](quick-start.md)相同的端口翻转流水线。非对称完全依赖链路损伤实现，与数据平面无关。

## 关键设计点

- **方向语义由 `(a, b)` 顺序决定**。`Link(h1, s1, delay_a_to_b="200ms")` 整形 h1→s1。如果交换写法为 `Link(s1, h1, ...)`，同样的 `delay_a_to_b` 整形的将是 s1→h1。`add_link(a, b, ...)` 的参数顺序就是基准。
- **同一参数同时给对称值与非对称值会被构造期拒绝**。不能既写 `delay="50ms"` 又写 `delay_a_to_b="100ms"`——同一个参数只能二选一。但跨参数自由组合是允许的（例如对称 `bandwidth` 加非对称 `delay`）。

## 可尝试的变体

- 把 `h1` 到 `s1` 链路上的 `delay_a_to_b="200ms"` 换成 `loss_pct_a_to_b=50.0`，让这个方向的报文有 50% 的概率被丢弃。运行 `h1 ping h2 10 1`，查看 10 次探测的实测丢包率。`pingall 10 1` 矩阵只显示每个主机对是否至少收到一次应答。
- 给 `h1` 和 `s1` 之间的链路加上 `delay_b_to_a="200ms"`，再去掉 `h2` 和 `s1` 之间的 `delay_b_to_a="20ms"`。此时两个方向各增加 200 ms 时延，ping 的 RTT 约为 400 ms。
- 用非对称延迟搭配对称带宽整形（`bandwidth="1mbit"`）模拟典型的家用宽带链路。
