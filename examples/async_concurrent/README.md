# Async concurrent multi-switch table programming

Three switches form a full mesh. The example uses `AsyncP4RuntimeClient`
and `asyncio.gather()` to populate their IPv4 LPM tables concurrently.

## What this demonstrates

- `AsyncP4RuntimeClient` from `p4net.control` — the async parallel to
  the sync `P4RuntimeClient` shipped since v1.0.
- The `RunningSwitch.async_client` lazy property — every running
  switch exposes a pre-configured async client; consumers don't have
  to construct one by hand.
- `asyncio.gather` patterns: connect three clients in parallel,
  install all entries in parallel, disconnect in parallel.

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

Three hosts on three /24s, fully meshed switches. Each switch loads
the same P4 binary; per-switch forwarding state is written from
Python at start-up.

`setup(net)` adds a `/32` on-link route and a static neighbor entry for
each peer so the hosts can send packets across subnets. The switches
forward those packets using the IPv4 destination address.

## The async setup function

```python
async def _async_setup(net: Network) -> dict[str, Any]:
    """Concurrent table programming across all three switches.

    Returns a dict with timing measurements so the example can be
    instrumented from the shell or a test harness.
    """
    switches = [net.switch(n) for n in ("s1", "s2", "s3")]
    clients = [sw.async_client for sw in switches]

    # Connect all clients concurrently.
    await asyncio.gather(*(c.connect() for c in clients))

    # Build the full set of insert coroutines, then gather them.
    tasks = []
    for sw, client in zip(switches, clients, strict=True):
        for cidr, port in _ROUTES[sw.name]:
            tasks.append(
                client.insert_table_entry(
                    "MyIngress.ipv4_lpm",
                    {"hdr.ipv4.dstAddr": cidr},
                    "MyIngress.set_egress_port",
                    {"port": port},
                )
            )
    import time as _time

    t0 = _time.perf_counter()
    await asyncio.gather(*tasks)
    t1 = _time.perf_counter()
    concurrent_ms = (t1 - t0) * 1000.0

    # Disconnect (Network.stop would do this anyway).
    await asyncio.gather(*(c.disconnect() for c in clients))
    return {"concurrent_ms": concurrent_ms, "inserts": len(tasks)}
```

The full source is in [`topology.py`](topology.py).

## Run it

From the repository root, start the topology.

```sh
sudo p4net examples/async_concurrent/topology.py
```

The example closes the async connections after all nine writes finish.
With `-v`, each client logs `disconnected`. The entries stay on the
switches for forwarding. The example then prints a timing summary like
this.

```
async setup: 9 table entries installed concurrently in 4.04 ms
```

At the `p4net>` prompt, run `pingall`.

```text
pingall
```

The matrix reports `6/6 succeeded`, covering both directions between
each pair of hosts.

Enter `exit` at the `p4net>` prompt to stop the topology.

## Timing the inserts

The timer starts after the clients connect and stops after all nine
insert calls return. To compare serial and concurrent writes, reuse
the same connected clients and clear the tables between trials. Time
a fresh set of the same nine inserts each time. For the serial trial,
await each insert before starting the next.

## When to use async

- **Concurrent operations across switches** — use `asyncio.gather()`
  to overlap independent RPCs.
- **Async PacketIn handlers** — register an async callback with
  `client.on_packet_in(...)` and propagate naturally through your
  async pipeline.
- **Highly responsive CLI tools** — when concurrent reads of multiple
  switches matter.

`grpc.aio` uses background threads. If your experiment forks worker
processes, start them before creating gRPC clients, then create and
connect the clients inside each worker. See
[gRPC and worker processes](https://zhh2001.github.io/p4net/known-limitations/#1-grpc-connections-and-process-creation).

## When to use the synchronous client

- **One operation at a time** in a synchronous script. Use
  `switch.client` to call the synchronous API.
- **Direct mappings to the v1.0 sync API** in existing code. The sync
  client is the v1.0 baseline; the async client is also Stable since
  1.7.0 — pick whichever fits your codebase.

## Mastership note

The sync client at `switch.client` and the async client at
`switch.async_client` use separate connections and election IDs. A
client with a higher election ID can take over as primary. In this
example, the async clients obtain primary status before writing the
entries. To choose an election ID in your own program, see
[Connections and primary status](https://zhh2001.github.io/p4net/async-client/#connections-and-primary-status).
