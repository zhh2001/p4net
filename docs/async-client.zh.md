---
description: AsyncP4RuntimeClient 是基于 grpc.aio 的异步 P4Runtime 客户端，与同步客户端并行存在。1.x 自 1.7.0 起为「稳定」。
---

# 异步客户端

`AsyncP4RuntimeClient`（1.6 引入）是 1.0 起就有的同步
`P4RuntimeClient` 的异步并行版。公开方法同名，全部
`async def`，底层走 `grpc.aio` 而非同步 gRPC 通道。

两个客户端互不依赖，可以并存。新代码起步推荐先用 `switch.client`
（同步 API）；当需要跨交换机并发、异步 PacketIn 处理器、或者要绕开
同步客户端的线程限制时，再用 `switch.async_client`。

`AsyncP4RuntimeClient` 在 1.x 内自 1.7.0 起为**稳定**等级（由临时
升级而来，依据真实使用反馈无不兼容调整需求）——详见
[API 稳定性](api-stability.md)。

## 为什么用异步

三个动机：

1. **跨交换机并发操作**。串行给 N 台交换机下发表项是 O(N × RTT)；
   用 `asyncio.gather` 把它压成 O(RTT) 加事件循环开销。
   [`async_concurrent`](examples/async-concurrent.md) 示例演示了
   这种加速模式。

2. **自然的异步 PacketIn 处理器**。用 `client.on_packet_in(...)`
   注册 `async def` 回调，传给后续异步流水线就行。同步客户端的
   回调跑在后台线程上；异步客户端的回调跑在调用方的事件循环上。

3. **绕过同步客户端的多线程-fork 陷阱**。同步 `P4RuntimeClient`
   起 gRPC 后台线程；在同一 Python 进程里再 `subprocess.run`
   可能死锁——参见[已知限制](known-limitations.md)。
   `grpc.aio` 不起后台线程，所以这个坑不适用。

## 快速上手

```python
import asyncio
from p4net import Network
from p4net.topo import Topology

topology = Topology()
# ... 构造 topology ...

async def main(net: Network) -> None:
    sw = net.switch("s1")
    client = sw.async_client

    async with client:
        await client.insert_table_entry(
            "MyIngress.ipv4_lpm",
            {"hdr.ipv4.dstAddr": "10.0.0.0/24"},
            "MyIngress.set_egress_port",
            {"port": 2},
        )
        async for entry in client.list_table_entries("MyIngress.ipv4_lpm"):
            print(entry)

with Network(topology) as net:
    asyncio.run(main(net))
```

## API 参考

请参见 `p4net.control.AsyncP4RuntimeClient` 的自动生成文档：构造器、
属性、生命周期、表 CRUD、计数器、寄存器、数据包 I/O。

方法名层面与同步 API 严格对应。同步 API 中返回 `list[dict]` 的流式
读取，异步 API 返回 `AsyncIterator[dict]`——`list_table_entries` 是
代表例子。`on_packet_in` 同步版接受
`Callable[[bytes, dict], None]`，异步版接受
`Callable[[bytes, dict], Awaitable[None]]`。

## 主控权与双客户端

每个 P4Runtime 客户端有自己的 election ID。BMv2 同一时刻只接受一个
**primary** 客户端；其它都是 secondary。同步客户端与异步客户端
**互相独立**——即使打到同一台交换机，它们各持自己的 election ID。

三种使用模式：

- **全同步（1.0 默认）**。建好 `Network`，所有操作走 `switch.client`。
  同步客户端的 election ID 是 `Network.start()` 的毫秒时间戳，默认
  就是 primary。

- **全异步**。每台交换机都走 `switch.async_client.connect()`。如果
  另有同步客户端在线，传 `election_id=(更大值, 0)`；否则首次
  arbitration 异步客户端自然占住 primary。

- **混合（显式 election ID）**。同步 primary 写、异步 secondary
  只读。构造异步客户端时传 `election_id=(0, 0)` 强制为 secondary；
  读仍然可用，写会抛 `NotPrimaryError`。

切勿用两个客户端同时往同一台交换机写 primary 操作。BMv2 会接受
最近收到的那一方为 primary 而拒掉另一方；实际表现是难以排查的
间歇失败。

## 取消与错误处理

`grpc.aio` 把远端错误以 `grpc.aio.AioRpcError` 抛出；异步客户端把
它们翻译成与同步客户端一致的异常体系（`DuplicateEntryError`、
`EntryNotFoundError`、`NotPrimaryError`、`PipelineError`、
`P4RuntimeError`）。

当一个正在飞的操作被取消时——通常是它的 owning task 被取消，或
`disconnect()` 与之并行执行——异步客户端抛
[`AsyncOperationCancelledError`](api-stability.md#p4netcontrol)，
继承自 `P4RuntimeError`。这让取消点能与连接失败区分开：

```python
try:
    await asyncio.wait_for(client.insert_table_entry(...), timeout=1.0)
except asyncio.TimeoutError:
    ...  # wait_for 超时
except AsyncOperationCancelledError:
    ...  # 正在飞的任务在 RPC 中途被取消
except P4RuntimeError:
    ...  # 其它客户端故障
```

`asyncio.CancelledError` 本身在调用方 task 层面取消时会原样向上
传播，不会被客户端拦截。

## 稳定性

异步 API 在 p4net 1.x 自 1.7.0 起为**稳定**。它在 1.6.0 作为「临时」
引入，在真实使用磨合期内未出现需要不兼容调整的情况，于 1.7.0 升级。
完整契约与升级理由见
[API 稳定性](api-stability.md)。
