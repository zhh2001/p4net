---
description: Connecting the async P4Runtime client, managing primary status, programming tables, receiving PacketIn, and handling errors.
---

# Async client

`AsyncP4RuntimeClient` uses `grpc.aio` for concurrent switch programming and PacketIn handling in an event loop. It shares table encodings and the exception hierarchy with `P4RuntimeClient`, but some methods, call patterns, and return types differ. It has been a [stable API](api-stability.md#p4netcontrol) in p4net 1.x since 1.7.0.

## Complete example

This program starts one switch and two hosts, inserts an IPv4 route, and reads it back. `Network.start()` compiles and installs the pipeline. The async client uses the sync client's P4Info index and a higher election ID to become primary.

Save the code as `async_demo.py` in the repository root:

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
    await asyncio.to_thread(net.start)
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

Run as root using the Python interpreter where p4net is installed:

```bash
sudo "$(command -v python)" async_demo.py
```

The output should contain a `MyIngress.ipv4_lpm` entry with action parameters `{'port': b'\x02'}`. See the [async concurrent example](examples/async-concurrent.md) for a full forwarding topology.

## Connections and primary status

`net.switch("s1").async_client` lazily creates and caches an unconnected client with the sync client's current P4Info index. Call `await client.connect()` before using it. Sync and async clients have separate channels and election IDs. Connecting one does not close the other.

Without an explicit `election_id`, the async client generates one from the current millisecond timestamp at its first connection. The sync client created by `Network` also uses a timestamp, so do not rely on construction order to assign primary status. Construct a client with an explicit election ID, as in the complete example, when you need to choose which client writes. Election IDs are compared as `(high, low)` pairs.

For a backup client that only reads, construct it with `election_id=(0, 0)`. This value permits a backup connection. With other election IDs, an unsuccessful initial arbitration makes `connect()` raise `NotPrimaryError` and close the connection.

`client.is_primary` reflects the latest arbitration response. After losing primary status, a connected client can still read tables, while P4Runtime writes raise `NotPrimaryError`. `client.is_connected` is a Boolean property, used without parentheses.

## Differences from the sync API

| Operation | Sync client | Async client |
| --- | --- | --- |
| Connect and disconnect | `connect()`, `disconnect()` | `await connect()`, `await disconnect()` |
| Table writes | Direct calls | Same method names, used with `await` |
| Table reads | `list_table_entries()` returns a list | Returns an async iterator, consumed with `async for` without a preceding `await` |
| P4Info index | `client.index` | `client.info_index` |
| Pipeline installation | `set_pipeline_config()` accepts file paths | `await push_pipeline()` accepts P4Info text and BMv2 JSON as bytes |
| One counter cell | `CounterData`, with packet and byte counts | `int`, containing only the packet count |
| All counter cells | `{index: CounterData}` | `{index: packet_count}` |
| PacketIn handlers | Regular functions, called on the receiver thread | Async functions, awaited on the event loop |

The async client currently has no `get_pipeline_config()`, `clear_table()`, `reset_counter()`, or multicast group operations. See the [control-plane API](api/control.md) for full signatures.

When installing a pipeline yourself, pass text-format P4Info bytes to `push_pipeline()`, rather than a binary protobuf. The client replaces its P4Info index only after a successful response, retaining the previous index after failure or cancellation. Register operations use `simple_switch_CLI`; install that tool and pass `thrift_address` when constructing the client.

## Concurrent operations and PacketIn

For multiple connected primary clients, table writes can run concurrently:

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

This overlaps the time spent waiting for RPC responses across switches. Total time depends on the number of requests and the switches' processing speed.

Both `on_packet_in()` and its returned unsubscribe function require `await`. This function registers a handler and removes it after waiting for one PacketIn:

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

The P4 program must send packets to the CPU port; see the [CPU punt example](examples/cpu-punt.md). The receiver task awaits handlers in sequence. Send lengthy work to a queue or another task.

## Errors, timeouts, and cancellation

RPC failures are translated into p4net exceptions, with the original `grpc.aio.AioRpcError` available through `__cause__`:

| RPC status | Exception |
| --- | --- |
| `PERMISSION_DENIED` | `NotPrimaryError` |
| `UNAVAILABLE` | `ConnectionError` |
| `ALREADY_EXISTS` | `DuplicateEntryError` |
| `NOT_FOUND` | `EntryNotFoundError` |
| `CANCELLED` | `AsyncOperationCancelledError` |
| Other error statuses | `P4RuntimeError` |

For `push_pipeline()`, `INVALID_ARGUMENT` and `FAILED_PRECONDITION` instead raise `PipelineError`. For table and counter requests, `FAILED_PRECONDITION` is a regular `P4RuntimeError`, for example when no forwarding pipeline is configured.

When an outer `UNKNOWN` response contains per-update P4Runtime errors, the client selects the first recognized non-success code. If no such code can be decoded, it raises `P4RuntimeError`.

Cancellation and timeout results depend on the operation. The timeout column refers to the method's own `timeout=` parameter:

| Operation | Calling task cancelled | Timeout |
| --- | --- | --- |
| `connect()` | Cleans up, then propagates `asyncio.CancelledError` | Arbitration timeout raises `ConnectionError` |
| `push_pipeline()`, table writes | `AsyncOperationCancelledError` | `asyncio.TimeoutError` |
| Iterating `list_table_entries()` | `asyncio.CancelledError` | `P4RuntimeError`, caused by RPC status `DEADLINE_EXCEEDED` |
| `read_counter()` | `AsyncOperationCancelledError` | `P4RuntimeError`, caused by RPC status `DEADLINE_EXCEEDED` |
| Register operations, `expect_packet_in()` | `AsyncOperationCancelledError` | `P4RuntimeError` |

An outer `asyncio.wait_for()` cancels the operation it is awaiting, so its final exception also depends on that operation's cancellation handling.

## Shutting down a network

Connect, use, and close an async client on the same event loop. The [gRPC AsyncIO documentation](https://grpc.github.io/grpc/python/grpc_asyncio.html#caveats) also requires async objects to be used on the thread where they were created. `Network.start()` is synchronous; use `await asyncio.to_thread(net.start)` inside a coroutine.

`await net.astop()` closes the network's cached async clients before releasing network resources. Close clients you construct yourself with `async with` or `await client.disconnect()`, as the complete example does. Keep the clients' event loops running until shutdown finishes. If `astop()` is cancelled, it finishes cleanup before propagating `CancelledError`.

Outside an event loop, `net.stop()` waits for cleanup. Inside an event loop, it starts cleanup in a background thread and returns. Use `await net.astop()` when you need to wait for shutdown.

When the StreamChannel ends or fails, the client clears its connection and primary status and closes the channel. Call `connect()` on the same instance to reconnect. If the stream ends during initial arbitration, the connection wait ends immediately. `connect()` and `disconnect()` wait for any cleanup already in progress.

`grpc.aio` still uses background threads. When creating worker processes, start the processes before opening gRPC connections in each process. See [gRPC's fork documentation](https://github.com/grpc/grpc/blob/master/doc/fork_support.md).
