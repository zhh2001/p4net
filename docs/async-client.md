---
description: AsyncP4RuntimeClient is a parallel async P4Runtime client based on grpc.aio. Stable in 1.x since 1.7.0; mirrors the sync client's API.
---

# Async client

`AsyncP4RuntimeClient` (added in 1.6) is the async parallel to the sync `P4RuntimeClient` shipped since 1.0. Same public method names, every method `async def`, backed by `grpc.aio` instead of the sync gRPC channel.

The two clients are independent and can coexist. The sync API at `switch.client` remains the recommended starting point for new code; the async API at `switch.async_client` is the right choice when you need concurrency across switches, async PacketIn handlers, or want to avoid the sync client's threading-related caveats.

`AsyncP4RuntimeClient` is **Stable** in p4net 1.x since version 1.7.0 (promoted from Provisional after empirical user soak — see the [API Stability](api-stability.md) page for the contract).

## Why async

Three reasons to reach for the async API:

1. **Concurrent operations across switches.** Programming N switches' forwarding tables sequentially is O(N × RTT); using `asyncio.gather` collapses that to O(RTT) plus event-loop overhead. The [`async_concurrent`](examples/async-concurrent.md) example shows the speedup pattern in practice.

2. **Natural async PacketIn handlers.** Register an `async def` callback with `client.on_packet_in(...)` and propagate values through the rest of your async pipeline. The sync client's handlers run on a background thread; the async client's run on your event loop.

3. **Bypasses the sync client's multi-threaded-fork pathology.** The sync `P4RuntimeClient` spawns gRPC background threads; combining that with `subprocess.run` in the same Python process can deadlock — see [Known Limitations](known-limitations.md). `grpc.aio` doesn't spawn background threads, so the trap doesn't apply.

## Quick start

```python
import asyncio
from p4net import Network
from p4net.topo import Topology

topology = Topology()
# ... configure topology ...

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

## Shutting down a network

Use `await net.astop()` when the network is managed inside an event loop:

```python
async def main() -> None:
    net = Network(topology)
    await asyncio.to_thread(net.start)
    try:
        client = net.switch("s1").async_client
        await client.connect()
        # ... use the network ...
    finally:
        await net.astop()

asyncio.run(main())
```

`astop()` closes each cached async client on the event loop where it connected, then tears down the network resources. It also closes clients still waiting for arbitration. Keep those event loops running until `astop()` finishes. If the shutdown task is cancelled, cleanup finishes before `CancelledError` propagates.

The synchronous `net.stop()` remains available. Outside an event loop, it waits for cleanup to finish. Inside an event loop, it starts cleanup in a background thread and returns. Use `await net.astop()` to wait for that cleanup. Both methods support repeated calls.

## API reference

See the generated docs for `p4net.control.AsyncP4RuntimeClient`: constructor, properties, lifecycle, table CRUD, counters, registers, packet I/O.

The API mirror is exact at the method-name level. Where the sync API returns a `list[dict]` for streaming reads, the async API returns an `AsyncIterator[dict]` — `list_table_entries` is the canonical example. Where the sync API takes a `Callable[[bytes, dict], None]` for `on_packet_in`, the async API takes a `Callable[[bytes, dict], Awaitable[None]]`.

## Mastership and dual clients

Each `P4Runtime` client owns an election ID. The BMv2 device accepts exactly one client as **primary** at any moment; others are secondary. The sync and async clients are **independent** — they each have their own election ID, even when they're hitting the same switch.

Three usage patterns:

- **All sync (the v1.0 default).** Build a `Network`, use `switch.client` for everything. The sync client's election ID is the millisecond-time-since-epoch of `Network.start()`, so it's primary by default.

- **All async.** Use `switch.async_client.connect()` for every switch. Pass `election_id=(higher_value, 0)` if the sync client is also connected somewhere; otherwise the async client takes primary on first arbitration.

- **Mixed with explicit election IDs.** Sync primary (writes); async secondary (reads only). Construct the async client with `election_id=(0, 0)` to force it into secondary; reads still work, writes raise `NotPrimaryError`.

Each arbitration notification updates `client.is_primary`: status `OK` sets it to `True`, and any other status sets it to `False`. The connection stays open when primary status changes. A client that loses primary status can still read, while writes raise `NotPrimaryError`.

## Cancellation and error handling

`grpc.aio` surfaces remote errors as `grpc.aio.AioRpcError`; the async client translates these into the same exception hierarchy as the sync client (`DuplicateEntryError`, `EntryNotFoundError`, `NotPrimaryError`, `PipelineError`, `P4RuntimeError`).

For an `UNKNOWN` response containing per-update P4Runtime errors, the client uses the first recognized failure code, just like the sync client. Duplicate entries raise `DuplicateEntryError`; missing entries raise `EntryNotFoundError`. Missing or malformed error details leave the error as `P4RuntimeError`. The original RPC error remains available as the exception's cause.

`push_pipeline()` classifies RPC failures by status and preserves the original RPC error as the exception's cause:

| RPC status | Exception |
| --- | --- |
| `PERMISSION_DENIED` | `NotPrimaryError` |
| `UNAVAILABLE` | `ConnectionError` |
| `CANCELLED` | `AsyncOperationCancelledError` |
| `INVALID_ARGUMENT`, `FAILED_PRECONDITION` | `PipelineError` |
| `NOT_FOUND` | `EntryNotFoundError` |
| `ALREADY_EXISTS` | `DuplicateEntryError` |
| Other error statuses | `P4RuntimeError` |

Failed or cancelled updates retain the current P4Info index. The index is replaced only after a successful response. Cancelling the calling task raises `AsyncOperationCancelledError`; expiration of `push_pipeline(timeout=...)` raises `asyncio.TimeoutError`.

If `connect()` is cancelled while waiting for arbitration, it closes its gRPC channel and stops its stream task before propagating `asyncio.CancelledError`. Cancellation by an outer `asyncio.wait_for()` follows the same cleanup path.

When the StreamChannel ends or fails, the client clears `is_connected` and `is_primary` and closes the channel. New operations raise `ConnectionError`. Call `connect()` to establish a new stream on the same client. If the stream ends during the initial arbitration, `connect()` fails without waiting for the arbitration timeout. If cleanup is still running, `connect()` and `disconnect()` wait for it to finish.

When an in-flight operation is cancelled — typically because the owning task was cancelled, or `disconnect()` was called concurrently — the async client raises [`AsyncOperationCancelledError`](api-stability.md#p4netcontrol), a subclass of `P4RuntimeError`. This lets cancellation sites distinguish a clean cancel from a connection failure:

```python
try:
    await asyncio.wait_for(client.insert_table_entry(...), timeout=1.0)
except asyncio.TimeoutError:
    ...  # the wait_for timeout
except AsyncOperationCancelledError:
    ...  # in-flight task was cancelled mid-RPC
except P4RuntimeError:
    ...  # any other client failure
```

## Stability

The async API is **Stable** in p4net 1.x since version 1.7.0. It was introduced as Provisional in 1.6.0 and promoted after real-world user soak surfaced no need for backwards-incompatible adjustments — see the [API Stability](api-stability.md#provisional-tier-and-async-client) page for the full contract and promotion rationale.
