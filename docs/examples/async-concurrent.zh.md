---
description: 三交换机网格示例，借助 v1.6 异步客户端并发下发转发表项。演示跨交换机的 asyncio.gather 模式。
---

# 异步并发多交换机

三交换机全网格拓扑，每台交换机的 IPv4 LPM 表通过 v1.6
`AsyncP4RuntimeClient` **并发**下发。可见的收益是耗时：三台交换机
并行下发比串行快了大约 gRPC RTT 倍数（减去事件循环开销）。

## 拓扑

`examples/async_concurrent/topology.py`：

```python
--8<-- "examples/async_concurrent/topology.py"
```

## P4 程序

`examples/async_concurrent/concurrent.p4`：

```p4
--8<-- "examples/async_concurrent/concurrent.p4"
```

标准 `ipv4_lpm` 表，流水线本身没有什么花活；有趣的部分在 Python
那一侧。

## 跑起来

```
sudo p4net examples/async_concurrent/topology.py
```

`setup(net)` 通过 `asyncio.run(_async_setup(net))` 并发连接三个异步
客户端、并发安装九条表项，并打印一行耗时。接着在 `p4net>` shell：

```
pingall
```

三台主机可以跨网格互 ping。

## 相关阅读

- [异步客户端](../async-client.md)——总览、主控权模式、取消语义。
- [API 稳定性](../api-stability.md)——`AsyncP4RuntimeClient` 在 1.x
  自 1.7.0 起为稳定等级。
