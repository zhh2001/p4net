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
  h1 (10.0.1.1/24)            h2 (10.0.2.1/24)            h3 (10.0.3.1/24)
      |                           |                           |
      | port 1                    | port 1                    | port 1
     s1 -------- port 2 ---------- s2 -------- port 3 -------- s3
      |                                                       |
      |                  port 3                       port 2  |
      +-------------------------------------------------------+
```

Three hosts on three /24s, fully meshed switches. Each switch loads
the same P4 binary; per-switch forwarding state is written from
Python at start-up.

`setup(net)` adds a `/32` on-link route and a static neighbor entry for
each peer so the hosts can send packets across subnets. The switches
forward those packets using the IPv4 destination address.

## The async setup function

```python
async def _async_setup(net: Network) -> dict:
    switches = [net.switch(n) for n in ("s1", "s2", "s3")]
    clients = [sw.async_client for sw in switches]

    # 1. Connect all three clients in parallel.
    await asyncio.gather(*(c.connect() for c in clients))

    # 2. Install every route (9 entries total: 3 subnets × 3 switches).
    tasks = []
    for sw, client in zip(switches, clients):
        for cidr, port in _ROUTES[sw.name]:
            tasks.append(client.insert_table_entry(
                "MyIngress.ipv4_lpm",
                {"hdr.ipv4.dstAddr": cidr},
                "MyIngress.set_egress_port",
                {"port": port},
            ))
    await asyncio.gather(*tasks)

    # 3. Disconnect (Network.stop would do this anyway, but explicit is fine).
    await asyncio.gather(*(c.disconnect() for c in clients))
```

The full source is in [`topology.py`](topology.py).

## Run it

```sh
sudo p4net examples/async_concurrent/topology.py
```

On startup, the example prints a line like this.

```
async setup: 9 table entries installed concurrently in 4.04 ms
```

Then in the `p4net>` shell:

```
pingall
```

The matrix reports `6/6 succeeded`, covering both directions between
each pair of hosts.

## Timing the inserts

The timer starts after the clients connect and stops after all nine
insert calls return. To compare serial and concurrent writes, reuse
the same connected clients and clear the tables between trials. Time
a fresh set of the same nine inserts each time. For the serial trial,
await each insert before starting the next.

## When to use async

- **Concurrent operations across switches** — use `asyncio.gather()`
  to overlap independent RPCs.
- **Multi-Network workflows in one Python process** — the sync
  client's gRPC threads tickle Python's multi-threaded-fork pathology
  (see [Known Limitations](https://zhh2001.github.io/p4net/known-limitations/));
  the async client uses `grpc.aio` which doesn't spawn background
  threads, so this trap doesn't apply.
- **Async PacketIn handlers** — register an async callback with
  `client.on_packet_in(...)` and propagate naturally through your
  async pipeline.
- **Highly responsive CLI tools** — when concurrent reads of multiple
  switches matter.

## When to use the synchronous client

- **One operation at a time** in a synchronous script. Use
  `switch.client` to call the synchronous API.
- **Direct mappings to the v1.0 sync API** in existing code. The sync
  client is the v1.0 baseline; the async client is also Stable since
  1.7.0 — pick whichever fits your codebase.

## Mastership note

The sync client at `switch.client` and the async client at
`switch.async_client` are *independent* connections with independent
election IDs. By default the sync client wins primary because it
connects first during `Network.start()`. If you want the async client
to be primary, construct it manually with a higher election_id, or
disconnect the sync client first. Mixing primary writes through both
clients against the same switch is not supported.
