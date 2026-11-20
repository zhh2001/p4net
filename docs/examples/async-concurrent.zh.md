---
description: 三台交换机组成网格，通过 AsyncP4RuntimeClient 和 asyncio.gather 并发下发 IPv4 转发表项。
---

# 异步并发多交换机

三台交换机两两相连。示例用 `AsyncP4RuntimeClient` 和 `asyncio.gather()` 并发下发各交换机的 IPv4 LPM 表项。

## 跑起来

在仓库根目录启动拓扑。

```sh
sudo p4net examples/async_concurrent/topology.py
```

`setup(net)` 先连接三台交换机，再计时下发这批表项。九条写入操作全部返回时，计时结束。在 `p4net>` 提示符下运行 `pingall`。

```
p4net> pingall
Pinging 6 host pairs...
2/6 host pairs checked
4/6 host pairs checked
H \ H  h1   h2   h3
h1     -    1    1
h2     1    -    1
h3     1    1    -
6/6 succeeded
```

矩阵显示 `6/6 succeeded`，表示每对主机的两个方向都连通。

完成后，在 `p4net>` 提示符下输入 `exit` 停止拓扑。

## 拓扑

`examples/async_concurrent/topology.py`：

```python
--8<-- "examples/async_concurrent/topology.py"
```

三台主机各在一个 IPv4 子网中。`setup(net)` 为每台主机配置到另外两台主机的 `/32` 直连路由和静态邻居项。交换机按 IPv4 目的地址在网格中转发。

## P4 程序

`examples/async_concurrent/concurrent.p4`：

```p4
--8<-- "examples/async_concurrent/concurrent.p4"
```

标准 `ipv4_lpm` 表，流水线本身没有什么花活；有趣的部分在 Python 那一侧。

## 相关阅读

- [异步客户端](../async-client.md)——总览、主控权模式、取消语义。
- [API 稳定性](../api-stability.md)——`AsyncP4RuntimeClient` 在 1.x 自 1.7.0 起为稳定等级。
