---
description: 异步 P4Runtime 客户端的连接、主控权、表项操作、PacketIn 和错误处理。
---

# 异步客户端

`AsyncP4RuntimeClient` 使用 `grpc.aio`，适合并发配置多台交换机，或在事件循环中接收 PacketIn。它与同步客户端 `P4RuntimeClient` 使用相同的表项编码和异常体系，但调用方式、部分方法和返回值不同。该接口自 1.7.0 起属于 1.x 的[稳定 API](api-stability.md#p4netcontrol)。

## 完整示例

下面的程序启动一台交换机和两台主机，写入一条 IPv4 路由并读回表项。`await net.astart()` 在工作线程中完成编译和流水线下发，异步客户端直接使用同步客户端的 P4Info 索引。示例使用较大的选举 ID，让异步客户端获得主控权。

在仓库根目录将代码保存为 `async_demo.py`：

```python
import asyncio
from pathlib import Path

from p4net import Network
from p4net.control import AsyncP4RuntimeClient
from p4net.topo import Topology

topology = Topology()
h1 = topology.add_host("h1", ip="10.0.1.1/24")
h2 = topology.add_host("h2", ip="10.0.2.1/24")
s1 = topology.add_switch("s1", p4_src=Path("examples/async_concurrent/concurrent.p4"))
topology.add_link(h1, s1, port_b=1)
topology.add_link(h2, s1, port_b=2)


async def main() -> None:
    net = Network(topology)
    await net.astart()
    try:
        sw = net.switch("s1")
        client = AsyncP4RuntimeClient(
            grpc_address=("127.0.0.1", sw.bmv2.grpc_port),
            device_id=sw.client.device_id,
            info_index=sw.client.index,
            thrift_address=("127.0.0.1", sw.bmv2.thrift_port),
            election_id=(sw.client.election_id[0] + 1, 0),
        )
        async with client:
            await client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": "10.0.2.0/24"},
                "MyIngress.set_egress_port",
                {"port": 2},
            )
            async for entry in client.list_table_entries("MyIngress.ipv4_lpm"):
                print(entry)
    finally:
        await net.astop()


asyncio.run(main())
```

使用已安装 p4net 的 Python，以 root 身份运行：

```bash
sudo "$(command -v python)" async_demo.py
```

输出中应有 `MyIngress.ipv4_lpm` 表项，动作参数为 `{'port': b'\x02'}`。完整拓扑转发配置可参见[异步并发示例](examples/async-concurrent.md)。

## 连接与主控权

`net.switch("s1").async_client` 会延迟创建并缓存一个尚未连接的客户端，创建时带入当前同步客户端的 P4Info 索引。调用 `await client.connect()` 后才能使用。同步和异步客户端各有自己的通道与选举 ID，连接其中一个不会关闭另一个。

未传入 `election_id` 时，异步客户端在首次连接时用当前毫秒时间戳生成选举 ID。`Network` 创建的同步客户端也使用时间戳，因此不要依赖创建顺序决定主控权。需要指定主控时，按完整示例自行构造客户端并传入选举 ID。选举 ID 按 `(high, low)` 比较。

若只需作为备用客户端读取，构造时设置 `election_id=(0, 0)`。该值允许客户端以备用身份完成连接。其他选举 ID 在初次仲裁未获得主控权时，`connect()` 会抛出 `NotPrimaryError` 并关闭连接。

`client.is_primary` 反映最近一次仲裁结果。连接后失去主控权仍可读表，P4Runtime 写请求会抛出 `NotPrimaryError`。`client.is_connected` 是布尔属性，使用时不加括号。

## 与同步接口的区别

| 操作 | 同步客户端 | 异步客户端 |
| --- | --- | --- |
| 连接与断开 | `connect()`、`disconnect()` | `await connect()`、`await disconnect()` |
| 表项写入 | 直接调用 | 同名方法，使用 `await` |
| 表项读取 | `list_table_entries()` 返回列表 | 返回异步迭代器，使用 `async for`，不先 `await` |
| P4Info 索引 | `client.index` | `client.info_index` |
| 流水线下发 | `set_pipeline_config()` 接收文件路径 | `await push_pipeline()` 接收 P4Info 文本和 BMv2 JSON 的字节内容 |
| 单个计数器单元 | `CounterData`，含包数和字节数 | `int`，仅返回包数 |
| 全部计数器单元 | `{索引: CounterData}` | `{索引: 包数}` |
| PacketIn 处理器 | 普通函数，在接收线程中调用 | 异步函数，在事件循环中等待执行 |

异步客户端目前没有 `get_pipeline_config()`、`clear_table()`、`reset_counter()` 或组播组操作。完整签名见[控制平面 API](api/control.md)。

自行下发流水线时，`push_pipeline()` 的第一个参数需要文本格式的 P4Info，不是二进制 protobuf。成功响应后才会替换客户端的 P4Info 索引，失败或取消时保留原索引。寄存器读写通过 `simple_switch_CLI` 执行，需要安装该工具，并在构造客户端时传入 `thrift_address`。

## 并发操作与 PacketIn

对已连接且拥有主控权的多个客户端，可并发下发表项：

```python
async def program_switches(clients: list[AsyncP4RuntimeClient]) -> None:
    await asyncio.gather(
        *(
            client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": "10.0.2.0/24"},
                "MyIngress.set_egress_port",
                {"port": 2},
            )
            for client in clients
        )
    )
```

这样可以让多个交换机的 RPC 同时等待响应，实际耗时取决于请求量和交换机处理速度。

`on_packet_in()` 和它返回的注销函数都需要 `await`。下面的函数注册处理器，等到一个 PacketIn 后注销：

```python
async def watch_packet(client: AsyncP4RuntimeClient) -> None:
    async def handle_packet(payload: bytes, metadata: dict[str, int]) -> None:
        print("PacketIn:", len(payload), metadata)

    unsubscribe = await client.on_packet_in(handle_packet)
    try:
        await client.expect_packet_in(timeout=5.0)
    finally:
        await unsubscribe()
```

P4 程序需要将数据包送到 CPU 端口，具体配置见 [CPU punt 示例](examples/cpu-punt.md)。处理器由消息接收任务依次等待执行，耗时工作可交给队列或其他任务。

## 错误、超时与取消

RPC 错误会转换成 p4net 异常，原始 `grpc.aio.AioRpcError` 保留在 `__cause__` 中：

| RPC 状态 | 异常 |
| --- | --- |
| `PERMISSION_DENIED` | `NotPrimaryError` |
| `UNAVAILABLE` | `ConnectionError` |
| `ALREADY_EXISTS` | `DuplicateEntryError` |
| `NOT_FOUND` | `EntryNotFoundError` |
| `CANCELLED` | `AsyncOperationCancelledError` |
| 其他错误状态 | `P4RuntimeError` |

`push_pipeline()` 另将 `INVALID_ARGUMENT` 和 `FAILED_PRECONDITION` 转为 `PipelineError`。表项或计数器请求的 `FAILED_PRECONDITION` 则是普通 `P4RuntimeError`，例如交换机尚未配置流水线时。

外层状态为 `UNKNOWN` 且携带逐条 P4Runtime 错误时，客户端使用第一个可识别的非成功状态码选择异常。无法解析出这样的状态码时，保留为 `P4RuntimeError`。

取消和超时的结果取决于具体操作。下表的超时指方法自身的 `timeout=` 参数：

| 操作 | 调用任务被取消 | 超时 |
| --- | --- | --- |
| `connect()` | 清理连接后传播 `asyncio.CancelledError` | 仲裁等待超时为 `ConnectionError` |
| `push_pipeline()`、表项写入 | `AsyncOperationCancelledError` | `asyncio.TimeoutError` |
| `list_table_entries()` 的迭代 | `asyncio.CancelledError` | `P4RuntimeError`，原始 RPC 状态为 `DEADLINE_EXCEEDED` |
| `read_counter()` | `AsyncOperationCancelledError` | `P4RuntimeError`，原始 RPC 状态为 `DEADLINE_EXCEEDED` |
| 寄存器操作、`expect_packet_in()` | `AsyncOperationCancelledError` | `P4RuntimeError` |

外层 `asyncio.wait_for()` 会取消正在等待的操作，其最终异常也受该操作的取消处理影响。

## 关闭网络

在协程中使用 `await net.astart()` 启动网络。它先在调用线程安装清理处理器、注册网络，再由工作线程执行启动。需要自动处理 SIGINT/SIGTERM 时，让事件循环运行在主线程。Python 只允许在[主线程注册信号处理器](https://docs.python.org/3/library/signal.html#signals-and-threads)。同步接口 `net.start()` 仍可使用。

`astart()` 被取消时，会等待启动线程结束并关闭网络，再传播 `CancelledError`。启动失败时会回收已创建的资源，并传播原始异常。在启动期间调用 `stop()` 或 `astop()`，清理会等待启动线程结束。如果启动尚未完成，启动调用会抛出 `NetworkError`。

在同一事件循环中连接、使用和关闭异步客户端。[gRPC AsyncIO 文档](https://grpc.github.io/grpc/python/grpc_asyncio.html#caveats)也要求异步对象在创建它们的线程中使用。

`await net.astop()` 会关闭网络缓存的异步客户端，再释放网络资源。自行构造的客户端需要用 `async with` 或 `await client.disconnect()` 关闭，完整示例使用了前一种方式。保持客户端所属的事件循环运行，直到关闭完成。`astop()` 被取消时会先完成清理，再传播 `CancelledError`。

`net.stop()` 在事件循环之外会等待清理完成，在事件循环内则启动后台清理并返回。需要等待关闭时，使用 `await net.astop()`。

事件循环运行时，SIGINT/SIGTERM 处理器会先等待网络清理，再恢复原处理器并重新投递信号。事件循环之外使用同步清理。

StreamChannel 结束或报错后，客户端会清除连接和主控状态并关闭通道。可在同一实例上调用 `connect()` 重连。初次仲裁期间断流会直接结束连接等待，清理进行中调用 `connect()` 或 `disconnect()` 会等待清理完成。

`grpc.aio` 仍使用后台线程。需要创建工作进程时，应先创建进程，再在各自进程中建立 gRPC 连接。参见 [gRPC 的 fork 说明](https://github.com/grpc/grpc/blob/master/doc/fork_support.md)。
