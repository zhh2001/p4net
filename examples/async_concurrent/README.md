# Async concurrent multi-switch table programming

Three-switch full-mesh topology where every switch's IPv4 LPM table is
populated **concurrently** via the v1.6 ``AsyncP4RuntimeClient``. The
visible payoff is timing: programming three switches in parallel beats
programming them sequentially by roughly the gRPC round-trip multiple,
minus event-loop overhead.

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

```
sudo p4net examples/async_concurrent/topology.py
```

The example prints a timing line from inside `setup(net)`:

```
async setup: 9 table entries installed concurrently in 4.27 ms
```

Then in the `p4net>` shell:

```
pingall
```

Hosts can ping each other across the mesh. The pipeline forwards IPv4
based on the LPM tables programmed by the async setup.

## Sample timing

Captured on the integration test rig (i5-13500H, WSL2, BMv2 1.15):

| Approach   | 9 inserts (3 switches × 3 routes) |
| ---------- | --------------------------------- |
| Concurrent | ~5 ms                             |
| Sequential | ~20 ms                            |
| Speedup    | ~4× (with sub-millisecond async event-loop overhead) |

Real BMv2 numbers vary run-to-run; expect a 2–5× speedup at this
problem size, growing with the number of switches.

## When to use async

- **Concurrent operations across switches** — the canonical case. N
  switches in parallel beats N sequential RPCs.
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

## When NOT to use async

- **Single-switch single-operation workflows** where simplicity matters
  more. The sync client at `switch.client` is just as fast against one
  switch and doesn't require an event loop.
- **Direct mappings to the v1.0 sync API** in existing code. The sync
  client is Stable; the async client is Provisional in 1.x — see the
  API stability page.

## Mastership note

The sync client at `switch.client` and the async client at
`switch.async_client` are *independent* connections with independent
election IDs. By default the sync client wins primary because it
connects first during `Network.start()`. If you want the async client
to be primary, construct it manually with a higher election_id, or
disconnect the sync client first. Mixing primary writes through both
clients against the same switch is not supported.
