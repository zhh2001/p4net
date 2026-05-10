---
description: 两台主机各自在同一 /24 与 /64 上配置 IPv4 与 IPv6。展示按接口的 IPv6 sysctl 门控。
---

# 双栈

一台交换机上的两台主机，各自既配 IPv4 `/24` 也配 IPv6 `/64`。
流水线与[快速上手](quick-start.md)相同——L2 端口翻转一视同仁
对待 v4 与 v6。本例的看点是地址管理。

## 你将看到什么

`pingall`（IPv4）与 `pingall6`（IPv6）都成功。主机接口上只有
我们显式声明的地址——没有 `fe80::` link-local 噪声，也没有
SLAAC 派生的地址。

## 拓扑

`examples/dual_stack/topology.py`：

```python
--8<-- "examples/dual_stack/topology.py"
```

`Host.ip` 与 `Host.ip6` 同时设置。编排器检测到这一点后，会在拉起
接口之前调用 `enable_ipv6(ns, iface)`（同时 `accept_ra=0`、
`autoconf=0`），然后把两个地址都赋上。

## P4 程序

`examples/dual_stack/dual_stack.p4`：

```p4
--8<-- "examples/dual_stack/dual_stack.p4"
```

流水线对 L3 无感——只做端口翻转。v4 与 v6 走完全相同的路径。

## 运行

```
sudo p4net examples/dual_stack/topology.py
```

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  fd00::1/64   h1-eth0
h2    10.0.0.2/24  fd00::2/64   h2-eth0

p4net> h1 cmd ip -6 addr show dev h1-eth0
3: h1-eth0@if4: <BROADCAST,MULTICAST,UP,LOWER_UP> ...
    inet6 fd00::1/64 scope global
       valid_lft forever preferred_lft forever

p4net> pingall
2/2 succeeded
p4net> pingall6
2/2 succeeded
```

注意：只有 `fd00::1/64`，没有 `fe80::` link-local——sysctl 门控
正在生效。

## 关键设计点

- **`accept_ra=0` 与 `autoconf=0`** 与 `disable_ipv6=0` 一同
  写入，使得内核不会偷偷地从 Router Advertisement 自动配置
  额外地址（这里没有 RA，但行为仍然要可预期）。
- **静态 ND** 在 `setup(net)` 中注入——关闭 `accept_ra` 后，
  IPv6 邻居发现仍然能工作，但每次冷启动 ping 都要走 ND 解析
  会拖慢测量。预先注入条目让延迟测量结果更干净。

## 可尝试的变体

- 把其中一台主机的 `ip6` 参数去掉，确认 `pingall6` 矩阵中确实
  排除了它（按 `primary_ip6` 过滤）。
- 在显式调用 `enable_ipv6(...)` 时把 `accept_ra=True`，观察会
  出现哪些地址（需要绕过编排器）。
- 加一个 `loss_pct=10.0` 的链路参数，观察 v4 与 v6 ping 得到
  相同的丢包率（qdisc 对 L3 无感）。
