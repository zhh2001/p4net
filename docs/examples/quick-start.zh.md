---
description: 最简单的 p4net 拓扑——一台交换机上的两台主机，使用写死的端口翻转流水线，无需运行时表项编程。
---

# 快速上手（端口翻转）

一台交换机上的两台主机，使用静态端口翻转流水线。无需运行时表项
编程。p4net 的 "Hello World"。

## 你将看到什么

两台主机间的 `pingall` 全部成功，数据平面是一个 30 行左右的 P4
程序，在端口 1 ↔ 2 之间无条件互换。

## 拓扑

`examples/quick_start/quick_start.py`：

```python
--8<-- "examples/quick_start/quick_start.py"
```

值得注意的几个点：

- `setup(net)` 是 `p4net` 控制台脚本在网络拉起后、Shell 进入前
  调用的钩子。这里把静态 ARP 注入到主机邻居缓存，避免第一次
  ICMP 触发地址解析。
- 同一份文件既能 `python quick_start.py` 直接运行（依赖
  `if __name__ == "__main__"` 块），也能 `p4net quick_start.py`
  运行（依赖模块级的 `topology` 与 `setup`）。

## P4 程序

`examples/quick_start/quick_start.p4`：

```p4
--8<-- "examples/quick_start/quick_start.p4"
```

入口控制根据 `ingress_port` 设置 `std.egress_spec`——没有表，
也不需要运行时控制平面。

## 运行

```
sudo p4net examples/quick_start/quick_start.py
```

Shell 中：

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  -            h1-eth0
h2    10.0.0.2/24  -            h2-eth0

p4net> pingall
H \ H   h1   h2
   h1    -    1
   h2    1    -
2/2 succeeded
```

## 关键设计点

- 这是最小的可运行程序。如果 `pingall` 在这里成功，说明工具链
  其余部分（`p4c`、BMv2、命名空间、veth 对、P4Runtime）都正常
  工作。
- 数据平面对 L3 一无所知——既没有 IPv4 报头解析，也没有 ARP
  逻辑。`setup(net)` 注入静态 ARP 使 L3 ping 能够成立。

## 可尝试的变体

- 在端口 3 上加一台主机。没有表项编程的话，发往端口 3 的包会
  落在隐式 drop 上（端口翻转只覆盖 1 ↔ 2）。
- 把条件分支换成单纯的 `mark_to_drop(std)`，观察 `pingall` 全是
  `X`。
- 给 `Link(..., loss_pct=20.0)`，跑 `pingall 10 1` 观察成功率
  下降。
