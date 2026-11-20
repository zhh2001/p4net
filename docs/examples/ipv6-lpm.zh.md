---
description: 为两台主机配置 IPv6 路由，并查看路由前缀和转发计数器。
---

# IPv6 LPM

两台 IPv6 主机接在同一台交换机上。通过 P4Runtime 将路由下发到 `ipv6_lpm` 表，再用 `table dump` 查看 `fd00::1/128` 这样的前缀。

## 运行

在仓库根目录启动拓扑。

```sh
sudo p4net examples/ipv6_lpm/topology.py
```

先查看两条 `/128` 路由，再用 `pingall6` 检查 IPv6 连通性，并读取出端口 2 的包计数器。

```
p4net> hosts
name  primary_ip  primary_ip6  interfaces
h1    -           fd00::1/64   h1-eth0
h2    -           fd00::2/64   h2-eth0

p4net> s1 table dump MyIngress.ipv6_lpm
MyIngress.ipv6_lpm (2 entries)
#0
  match:    hdr.ipv6.dstAddr=fd00::1/128
  action:   MyIngress.set_egress_port
  params:   port=1

#1
  match:    hdr.ipv6.dstAddr=fd00::2/128
  action:   MyIngress.set_egress_port
  params:   port=2

p4net> pingall6
H \ H  h1   h2
h1     -    1
h2     1    -
2/2 succeeded

p4net> s1 counter MyIngress.ipv6_pkts 2
pkts=2 bytes=236
```

完成后，在 `p4net>` 提示符下输入 `exit` 停止拓扑。

## 拓扑

`examples/ipv6_lpm/topology.py`：

```python
--8<-- "examples/ipv6_lpm/topology.py"
```

P4Net 在启用主机接口前开启 IPv6，并分别配置 `fd00::1/64` 和 `fd00::2/64`。`setup(net)` 添加静态 IPv6 邻居项，并为每台主机下发一条 `/128` 路由。

## P4 程序

`examples/ipv6_lpm/ipv6_lpm.p4`：

```p4
--8<-- "examples/ipv6_lpm/ipv6_lpm.p4"
```

LPM 表按 128 位 IPv6 目的地址匹配。`set_egress_port` 选择出端口，并累加该端口的间接计数器。

## 可尝试的变体

- 将端口 1 上的 `fd00::1/128` 换成 `fd00::/64`，并保留端口 2 上的 `fd00::2/128`。发往 `h2` 的报文仍优先命中 `/128` 路由。用 `pingall6` 确认结果为 `2/2 succeeded`。
- 在端口 3 上增加一台主机，地址设为 `fd00::3/64`，并给它单独分配 MAC 地址。在 `setup(net)` 中下发指向端口 3 的 `fd00::3/128` 路由，再补齐新主机与两台现有主机之间的双向静态 IPv6 邻居项。启动扩展后的拓扑，用 `pingall6` 确认结果为 `6/6 succeeded`。
- 用 Python 的 `client.read_counter("MyIngress.ipv6_pkts", index=2)` 读取端口 2 的计数器。
