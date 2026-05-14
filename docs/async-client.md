---
description: AsyncP4RuntimeClient is a parallel async P4Runtime client based on grpc.aio. Provisional in 1.x; mirrors the sync client's API.
---

# Async client

`AsyncP4RuntimeClient` (added in 1.6) is the async parallel to the
sync `P4RuntimeClient` shipped since 1.0. Same public method names,
every method `async def`, backed by `grpc.aio` instead of the sync
gRPC channel.

The two clients are independent and can coexist. The sync API at
`switch.client` remains the recommended starting point for new code;
the async API at `switch.async_client` is the right choice when you
need concurrency across switches, async PacketIn handlers, or want to
avoid the sync client's threading-related caveats.

`AsyncP4RuntimeClient` is **Provisional** in p4net 1.x — see
[API Stability](api-stability.md) for the upgrade-to-Stable
commitment.

## Why async

Three reasons to reach for the async API:

1. **Concurrent operations across switches.** Programming N switches'
   forwarding tables sequentially is O(N × RTT); using
   `asyncio.gather` collapses that to O(RTT) plus event-loop
   overhead. The [`async_concurrent`](examples/async-concurrent.md)
   example shows the speedup pattern in practice.

2. **Natural async PacketIn handlers.** Register an `async def`
   callback with `client.on_packet_in(...)` and propagate values
   through the rest of your async pipeline. The sync client's
   handlers run on a background thread; the async client's run
   on your event loop.

3. **Bypasses the sync client's multi-threaded-fork pathology.** The
   sync `P4RuntimeClient` spawns gRPC background threads; combining
   that with `subprocess.run` in the same Python process can
   deadlock — see
   [Known Limitations](known-limitations.md). `grpc.aio` doesn't
   spawn background threads, so the trap doesn't apply.

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

## API reference

See the generated docs for `p4net.control.AsyncP4RuntimeClient`:
constructor, properties, lifecycle, table CRUD, counters, registers,
packet I/O.

The API mirror is exact at the method-name level. Where the sync API
returns a `list[dict]` for streaming reads, the async API returns an
`AsyncIterator[dict]` — `list_table_entries` is the canonical
example. Where the sync API takes a `Callable[[bytes, dict], None]`
for `on_packet_in`, the async API takes a
`Callable[[bytes, dict], Awaitable[None]]`.

## Mastership and dual clients

Each `P4Runtime` client owns an election ID. The BMv2 device accepts
exactly one client as **primary** at any moment; others are
secondary. The sync and async clients are **independent** — they
each have their own election ID, even when they're hitting the same
switch.

Three usage patterns:

- **All sync (the v1.0 default).** Build a `Network`, use
  `switch.client` for everything. The sync client's election ID is
  the millisecond-time-since-epoch of `Network.start()`, so it's
  primary by default.

- **All async.** Use `switch.async_client.connect()` for every
  switch. Pass `election_id=(higher_value, 0)` if the sync client
  is also connected somewhere; otherwise the async client takes
  primary on first arbitration.

- **Mixed with explicit election IDs.** Sync primary (writes); async
  secondary (reads only). Construct the async client with
  `election_id=(0, 0)` to force it into secondary; reads still work,
  writes raise `NotPrimaryError`.

Do not mix primary writes through both clients against the same
switch. The BMv2 will accept whichever arrived most recently as
primary and reject the other; in practice this produces confusing
intermittent failures.

## Cancellation and error handling

`grpc.aio` surfaces remote errors as `grpc.aio.AioRpcError`; the
async client translates these into the same exception hierarchy as
the sync client (`DuplicateEntryError`, `EntryNotFoundError`,
`NotPrimaryError`, `PipelineError`, `P4RuntimeError`).

When an in-flight operation is cancelled — typically because the
owning task was cancelled, or `disconnect()` was called concurrently
— the async client raises
[`AsyncOperationCancelledError`](api-stability.md#p4netcontrol),
a subclass of `P4RuntimeError`. This lets cancellation sites
distinguish a clean cancel from a connection failure:

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

`asyncio.CancelledError` itself propagates through the client
unchanged for tasks the caller cancels at the task level (rather
than inside the RPC).

## Provisional status

The async API is Provisional in 1.x. Concretely:

- The class will not be removed in 1.x.
- Method names won't be renamed.
- Common-path behavior won't change incompatibly.

What may evolve:

- New exception types for edge cases that surface in practice.
- Precise semantics of cancellation, error chaining, context manager
  exit.
- Iteration behavior for streaming methods.

The commitment is to upgrade `AsyncP4RuntimeClient` to **Stable**
within two 1.x minor releases of its introduction, conditional on no
backwards-incompatible adjustments having been needed.
