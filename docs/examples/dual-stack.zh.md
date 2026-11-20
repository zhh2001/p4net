---
description: 将两台双栈主机接到同一台交换机，验证 IPv4 和 IPv6 连通性。
---

# 双栈

一台交换机上的两台主机，各自既配 IPv4 `/24` 也配 IPv6 `/64`。流水线与[快速上手](quick-start.md)相同——L2 端口翻转一视同仁对待 v4 与 v6。本例的看点是地址管理。

## 运行

在仓库根目录启动拓扑。

```sh
sudo p4net examples/dual_stack/topology.py
```

用 `pingall` 检查 IPv4，用 `pingall6` 检查 IPv6。两条命令都会报告 `2/2 succeeded`。

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  fd00::1/64   h1-eth0
h2    10.0.0.2/24  fd00::2/64   h2-eth0

p4net> h1 cmd ip -6 addr show dev h1-eth0
3: h1-eth0@if4: <BROADCAST,MULTICAST,UP,LOWER_UP> ...
    inet6 fd00::1/64 scope global
       valid_lft forever preferred_lft forever
    inet6 fe80::200:ff:fe00:1/64 scope link
       valid_lft forever preferred_lft forever

p4net> pingall
H \ H  h1   h2
h1     -    1
h2     1    -
2/2 succeeded

p4net> pingall6
H \ H  h1   h2
h1     -    1
h2     1    -
2/2 succeeded
```

`fd00::1/64` 来自拓扑配置。这次运行中，Linux 还生成了链路本地地址 `fe80::200:ff:fe00:1/64`。是否生成此类地址取决于内核的 `addr_gen_mode` 设置。

完成后，在 `p4net>` 提示符下输入 `exit` 停止拓扑。

## 拓扑

`examples/dual_stack/topology.py`：

```python
--8<-- "examples/dual_stack/topology.py"
```

两台主机都设置了 `Host.ip` 和 `Host.ip6`。p4net 会在启动接口前，为配置了 IPv6 地址的接口启用 IPv6。

## P4 程序

`examples/dual_stack/dual_stack.p4`：

```p4
--8<-- "examples/dual_stack/dual_stack.p4"
```

流水线对 L3 无感——只做端口翻转。v4 与 v6 走完全相同的路径。

## 关键设计点

- p4net 设置 `accept_ra=0` 来忽略路由器通告，设置 `autoconf=0` 来关闭基于通告前缀的地址自动配置。
- `setup(net)` 预设 IPv4 和 IPv6 静态邻居项，第一次 ping 就可以使用已配置的 MAC 地址。

## 可尝试的变体

- 把其中一台主机的 `ip6` 参数去掉，确认 `pingall6` 矩阵中确实排除了它（按 `primary_ip6` 过滤）。
- 给 `h1` 和 `s1` 之间的链路加上 `loss_pct=10.0`，比较 `h1 ping h2 10 1` 和 `h1 ping6 h2 10 1` 的丢包统计。链路对 IPv4 和 IPv6 报文使用相同的丢包概率，每轮测得的丢包率可能不同。
