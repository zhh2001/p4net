# Async concurrent multi-switch table programming

Three switches form a full mesh. The example uses `AsyncP4RuntimeClient` and `asyncio.gather()` to populate their IPv4 LPM tables concurrently.

## What this demonstrates

- `AsyncP4RuntimeClient` from `p4net.control` — the async parallel to the sync `P4RuntimeClient` shipped since v1.0.
- The `RunningSwitch.async_client` lazy property — every running switch exposes a pre-configured async client; consumers don't have to construct one by hand.
- `asyncio.gather` patterns: connect three clients in parallel, install all entries in parallel, disconnect in parallel.

## Topology

```
h1 (10.0.1.1/24)               h2 (10.0.2.1/24)               h3 (10.0.3.1/24)
        |                              |                              |
     port 1                         port 1                         port 1
        |                              |                              |
       s1 (port 2) --------- (port 2) s2 (port 3) --------- (port 2) s3
        |                                                             |
     port 3                                                        port 3
        |                                                             |
        +-------------------------------------------------------------+
```

Three hosts on three /24s, fully meshed switches. Each switch loads the same P4 binary; per-switch forwarding state is written from Python at start-up.

`setup(net)` adds a `/32` on-link route and a static neighbor entry for each peer so the hosts can send packets across subnets. The switches forward those packets using the IPv4 destination address.

## The async setup function

```python
async def _run_batch(*operations: Coroutine[Any, Any, None]) -> None:
    """Cancel and await unfinished operations if the batch fails."""
    tasks = [asyncio.create_task(operation) for operation in operations]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _async_setup(net: Network) -> dict[str, Any]:
    """Concurrent table programming across all three switches.

    Returns a dict with timing measurements so the example can be
    instrumented from the shell or a test harness.
    """
    switches = [net.switch(n) for n in ("s1", "s2", "s3")]
    clients = [sw.async_client for sw in switches]

    import time as _time

    try:
        # Connect all clients concurrently.
        await _run_batch(*(c.connect() for c in clients))

        writes = []
        for sw, client in zip(switches, clients, strict=True):
            for cidr, port in _ROUTES[sw.name]:
                writes.append(
                    client.insert_table_entry(
                        "MyIngress.ipv4_lpm",
                        {"hdr.ipv4.dstAddr": cidr},
                        "MyIngress.set_egress_port",
                        {"port": port},
                    )
                )

        t0 = _time.perf_counter()
        await _run_batch(*writes)
        concurrent_ms = (_time.perf_counter() - t0) * 1000.0
    finally:
        # Close every client before asyncio.run() shuts down their event loop.
        disconnect_results = await asyncio.gather(
            *(c.disconnect() for c in clients), return_exceptions=True
        )

    for result in disconnect_results:
        if isinstance(result, BaseException):
            raise result
    return {"concurrent_ms": concurrent_ms, "inserts": len(writes)}
```

The full source is in [`topology.py`](topology.py).

## Run it

From the repository root, start the topology.

```sh
sudo p4net examples/async_concurrent/topology.py
```

The example closes the async connections after all nine writes finish. With `-v`, each client logs `disconnected`. The entries stay on the switches for forwarding. The example then prints a timing summary like this.

```
async setup: 9 table entries installed concurrently in 4.04 ms
```

At the `p4net>` prompt, run `pingall`.

```text
pingall
```

The matrix reports `6/6 succeeded`, covering both directions between each pair of hosts.

Enter `exit` at the `p4net>` prompt to stop the topology.

## Timing the inserts

The example times the nine concurrent inserts. To compare them with serial inserts, replace the block from `writes = []` through the `concurrent_ms` assignment in `_async_setup` with the following code. Keep it inside the existing `try` block.

```python
        table = "MyIngress.ipv4_lpm"
        action = "MyIngress.set_egress_port"
        writes = [
            (client, {"hdr.ipv4.dstAddr": cidr}, port)
            for sw, client in zip(switches, clients, strict=True)
            for cidr, port in _ROUTES[sw.name]
        ]

        t0 = _time.perf_counter()
        for client, match, port in writes:
            await client.insert_table_entry(table, match, action, {"port": port})
        serial_ms = (_time.perf_counter() - t0) * 1000.0

        for client, match, _ in writes:
            await client.delete_table_entry(table, match)

        t0 = _time.perf_counter()
        await _run_batch(
            *(
                client.insert_table_entry(table, match, action, {"port": port})
                for client, match, port in writes
            )
        )
        concurrent_ms = (_time.perf_counter() - t0) * 1000.0

        print(f"serial setup: {len(writes)} table entries installed in {serial_ms:.2f} ms")
```

Start a fresh topology with the command above so the first trial begins with empty LPM tables. Each delete uses the same switch and match key as the corresponding insert, leaving the tables empty for the second trial. Both trials reuse the same three connections and time only the insert batches. Startup prints the serial time followed by the existing concurrent timing summary.

## When to use async

- **Concurrent operations across switches** — use `asyncio.gather()` to overlap independent RPCs.
- **Async PacketIn handlers** — register an async callback with `client.on_packet_in(...)` and propagate naturally through your async pipeline.
- **Highly responsive CLI tools** — when concurrent reads of multiple switches matter.

`grpc.aio` uses background threads. If your experiment forks worker processes, start them before creating gRPC clients, then create and connect the clients inside each worker. See [gRPC and worker processes](https://zhh2001.github.io/p4net/known-limitations/#1-grpc-connections-and-process-creation).

## When to use the synchronous client

- **One operation at a time** in a synchronous script. Use `switch.client` to call the synchronous API.
- **Direct mappings to the v1.0 sync API** in existing code. The sync client is the v1.0 baseline; the async client is also Stable since 1.7.0 — pick whichever fits your codebase.

## Mastership note

The sync client at `switch.client` and the async client at `switch.async_client` use separate connections and election IDs. A client with a higher election ID can take over as primary. In this example, the async clients obtain primary status before writing the entries. To choose an election ID in your own program, see [Connections and primary status](https://zhh2001.github.io/p4net/async-client/#connections-and-primary-status).
