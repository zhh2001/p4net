---
description: 为两台 IPv4 主机下发路由，并在 p4net 命令行验证最长前缀匹配。
---

# L3 转发

两台主机分别接入同一台 BMv2 交换机的不同端口。启动时，Python 为每台主机在交换机的 `ipv4_lpm` 表中下发一条路由。

## 运行

在仓库根目录启动拓扑。

```sh
sudo p4net examples/l3_forwarding/topology.py
```

在 `p4net>` 提示符下，先查看启动时下发的两条 `/32` 路由，再检查连通性并读取出端口 1 的包计数器。

```
p4net> s1 table dump MyIngress.ipv4_lpm
MyIngress.ipv4_lpm (2 entries)
#0
  match:    hdr.ipv4.dstAddr=10.0.0.1/32
  action:   MyIngress.set_egress_port
  params:   port=1

#1
  match:    hdr.ipv4.dstAddr=10.0.0.2/32
  action:   MyIngress.set_egress_port
  params:   port=2

p4net> pingall
H \ H  h1   h2
h1     -    1
h2     1    -
2/2 succeeded

p4net> s1 counter MyIngress.ingress_pkts 1
pkts=2 bytes=196
```

完成后，在 `p4net>` 提示符下输入 `exit` 停止拓扑。

## 拓扑

`examples/l3_forwarding/topology.py`：

```python
--8<-- "examples/l3_forwarding/topology.py"
```

`setup(net)` 通过 `s1.client.insert_table_entry()` 为每台主机配置目的前缀和出端口。客户端根据 P4Info 编码匹配键和动作参数。

## P4 程序

`examples/l3_forwarding/ipv4_lpm.p4`：

```p4
--8<-- "examples/l3_forwarding/ipv4_lpm.p4"
```

入口处理只对 IPv4 包查询 `ipv4_lpm`。`setup(net)` 在两台主机上配置静态 ARP 条目，让它们提前知道对方的 MAC 地址。

## 可尝试的变体

### 验证最长前缀匹配

每条转发表项选择一个出端口。两条 `/32` 路由分别把发往 `h1` 和 `h2` 的包送到端口 1 和端口 2。启动原拓扑后，在 `p4net>` 提示符下添加一条覆盖范围更广的 `/24` 丢弃规则。

```text
s1 table add MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.0/24 action: MyIngress.drop
pingall
```

`pingall` 仍显示 `2/2 succeeded`，因为两条 `/32` 路由优先匹配。删除发往 `h2` 的路由后再检查一次。

```text
s1 table del MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.2/32
pingall
```

这次 `pingall` 显示 `0/2 succeeded`。发往 `h2` 的请求和应答都会命中 `/24` 丢弃规则。输入 `exit` 后重新启动原拓扑，即可恢复启动时下发的路由。

### 增加第三台主机

把 `h3` 接到交换机端口 3，地址设为 `10.0.0.3/24`，MAC 地址设为 `00:00:00:00:00:03`。在 `setup(net)` 中添加 `10.0.0.3/32` 路由，动作设为 `MyIngress.set_egress_port`，参数设为 `port=3`。补齐 `h3` 与两台现有主机之间的双向静态 ARP 条目。运行扩展后的拓扑，确认 `pingall` 显示 `6/6 succeeded`。
