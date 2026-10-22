---
description: AsyncP4RuntimeClient 是基于 grpc.aio 的异步 P4Runtime 客户端，与同步客户端并行存在。1.x 自 1.7.0 起为「稳定」。
---

# 异步客户端

`AsyncP4RuntimeClient`（1.6 引入）是 1.0 起就有的同步 `P4RuntimeClient` 的异步并行版。公开方法同名，全部 `async def`，底层走 `grpc.aio` 而非同步 gRPC 通道。

两个客户端互不依赖，可以并存。新代码起步推荐先用 `switch.client`（同步 API）；当需要跨交换机并发、异步 PacketIn 处理器、或者要绕开同步客户端的线程限制时，再用 `switch.async_client`。

`AsyncP4RuntimeClient` 在 1.x 内自 1.7.0 起为**稳定**等级（由临时升级而来，依据真实使用反馈无不兼容调整需求）——详见 [API 稳定性](api-stability.md)。

## 为什么用异步

三个动机：

1. **跨交换机并发操作**。串行给 N 台交换机下发表项是 O(N × RTT)；用 `asyncio.gather` 把它压成 O(RTT) 加事件循环开销。[`async_concurrent`](examples/async-concurrent.md) 示例演示了这种加速模式。

2. **自然的异步 PacketIn 处理器**。用 `client.on_packet_in(...)` 注册 `async def` 回调，传给后续异步流水线就行。同步客户端的回调跑在后台线程上；异步客户端的回调跑在调用方的事件循环上。

3. **绕过同步客户端的多线程-fork 陷阱**。同步 `P4RuntimeClient` 起 gRPC 后台线程；在同一 Python 进程里再 `subprocess.run` 可能死锁——参见[已知限制](known-limitations.md)。`grpc.aio` 不起后台线程，所以这个坑不适用。

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

## 关闭网络

在事件循环中管理网络时，使用 `await net.astop()` 关闭：

```python
async def main() -> None:
    net = Network(topology)
    await asyncio.to_thread(net.start)
    try:
        client = net.switch("s1").async_client
        await client.connect()
        # ... 使用网络 ...
    finally:
        await net.astop()

asyncio.run(main())
```

`astop()` 在各异步客户端建立连接时使用的事件循环中关闭连接，然后释放网络资源。仍在等待仲裁的连接也会关闭。在 `astop()` 完成前，保持这些事件循环运行。关闭任务被取消时，会先完成清理，再传播 `CancelledError`。

同步接口 `net.stop()` 继续保留。在事件循环之外调用时，它会等待清理完成。在事件循环内调用时，它会启动后台线程执行清理，然后返回。此时可再调用 `await net.astop()` 等待清理完成。两个接口均可重复调用。

## API 参考

请参见 `p4net.control.AsyncP4RuntimeClient` 的自动生成文档：构造器、属性、生命周期、表 CRUD、计数器、寄存器、数据包 I/O。

方法名层面与同步 API 严格对应。同步 API 中返回 `list[dict]` 的流式读取，异步 API 返回 `AsyncIterator[dict]`——`list_table_entries` 是代表例子。`on_packet_in` 同步版接受 `Callable[[bytes, dict], None]`，异步版接受 `Callable[[bytes, dict], Awaitable[None]]`。

## 主控权与双客户端

每个 P4Runtime 客户端有自己的 election ID。BMv2 同一时刻只接受一个 **primary** 客户端；其它都是 secondary。同步客户端与异步客户端**互相独立**——即使打到同一台交换机，它们各持自己的 election ID。

三种使用模式：

- **全同步（1.0 默认）**。建好 `Network`，所有操作走 `switch.client`。同步客户端的 election ID 是 `Network.start()` 的毫秒时间戳，默认就是 primary。

- **全异步**。每台交换机都走 `switch.async_client.connect()`。如果另有同步客户端在线，传 `election_id=(更大值, 0)`；否则首次 arbitration 异步客户端自然占住 primary。

- **混合（显式 election ID）**。同步 primary 写、异步 secondary 只读。构造异步客户端时传 `election_id=(0, 0)` 强制为 secondary；读仍然可用，写会抛 `NotPrimaryError`。

客户端每次收到仲裁通知都会更新 `is_primary`：状态码为 `OK` 时设为 `True`，其他状态码设为 `False`。主控身份变化不会关闭连接。失去主控身份后仍可读取，写入会抛出 `NotPrimaryError`。

## 取消与错误处理

`grpc.aio` 把远端错误以 `grpc.aio.AioRpcError` 抛出；异步客户端把它们翻译成与同步客户端一致的异常体系（`DuplicateEntryError`、`EntryNotFoundError`、`NotPrimaryError`、`PipelineError`、`P4RuntimeError`）。

当 `UNKNOWN` 响应包含逐条 P4Runtime 错误时，客户端与同步版一样，按第一个可识别的失败状态码选择异常。重复表项抛出 `DuplicateEntryError`，表项不存在时抛出 `EntryNotFoundError`。错误详情缺失或损坏时，保留为 `P4RuntimeError`。原始 RPC 错误保留为异常原因。

表项或计数器请求返回 `FAILED_PRECONDITION` 时，抛出 `P4RuntimeError` 并保留服务器说明。例如，尚未配置转发流水线时就会返回该状态。`PERMISSION_DENIED` 仍抛出 `NotPrimaryError`。

`push_pipeline()` 根据 RPC 状态区分错误，并将原始 RPC 错误保留为异常原因：

| RPC 状态 | 异常 |
| --- | --- |
| `PERMISSION_DENIED` | `NotPrimaryError` |
| `UNAVAILABLE` | `ConnectionError` |
| `CANCELLED` | `AsyncOperationCancelledError` |
| `INVALID_ARGUMENT`、`FAILED_PRECONDITION` | `PipelineError` |
| `NOT_FOUND` | `EntryNotFoundError` |
| `ALREADY_EXISTS` | `DuplicateEntryError` |
| 其他错误状态 | `P4RuntimeError` |

更新失败或被取消时，保留当前 P4Info 索引，仅在收到成功响应后替换。取消调用任务会抛出 `AsyncOperationCancelledError`；`push_pipeline(timeout=...)` 超时会抛出 `asyncio.TimeoutError`。

等待仲裁响应时取消 `connect()`，客户端会先关闭 gRPC 连接并停止后台消息接收任务，再传播 `asyncio.CancelledError`。外层 `asyncio.wait_for()` 超时导致的取消也会执行同样的清理。

StreamChannel 结束或报错后，客户端会清除 `is_connected` 和 `is_primary`，并关闭 gRPC 连接。此时发起新操作会抛出 `ConnectionError`，可调用 `connect()` 在同一客户端上建立新的消息流。初次仲裁期间断流时，`connect()` 会直接报错，无需等到仲裁超时。清理旧连接期间调用 `connect()` 或 `disconnect()`，会等待清理完成。

当一个正在飞的操作被取消时——通常是它的 owning task 被取消，或 `disconnect()` 与之并行执行——异步客户端抛 [`AsyncOperationCancelledError`](api-stability.md#p4netcontrol)，继承自 `P4RuntimeError`。这让取消点能与连接失败区分开：

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

## 稳定性

异步 API 在 p4net 1.x 自 1.7.0 起为**稳定**。它在 1.6.0 作为「临时」引入，在真实使用磨合期内未出现需要不兼容调整的情况，于 1.7.0 升级。完整契约与升级理由见 [API 稳定性](api-stability.md)。
