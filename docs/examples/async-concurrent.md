---
description: Three-switch mesh whose forwarding tables are programmed concurrently via the v1.6 async client. Demonstrates asyncio.gather across switches.
---

# Async concurrent multi-switch

Three-switch full-mesh topology where every switch's IPv4 LPM table is
populated **concurrently** via the v1.6 `AsyncP4RuntimeClient`. The
visible payoff is timing: programming three switches in parallel beats
sequential by roughly the gRPC round-trip multiple, minus event-loop
overhead.

## Topology

`examples/async_concurrent/topology.py`:

```python
--8<-- "examples/async_concurrent/topology.py"
```

## P4 program

`examples/async_concurrent/concurrent.p4`:

```p4
--8<-- "examples/async_concurrent/concurrent.p4"
```

Standard `ipv4_lpm` table; nothing exotic in the pipeline. The
interesting bit is on the Python side.

## Run it

```
sudo p4net examples/async_concurrent/topology.py
```

`setup(net)` calls `asyncio.run(_async_setup(net))` which connects
three async clients in parallel, installs nine table entries
concurrently, and prints a timing line. Then in the `p4net>` shell:

```
pingall
```

All three hosts can ping each other across the mesh.

## See also

- [Async client](../async-client.md) — overview, mastership patterns,
  cancellation semantics.
- [API Stability](../api-stability.md) — `AsyncP4RuntimeClient` is
  Stable in 1.x since 1.7.0.
