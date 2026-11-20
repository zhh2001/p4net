---
description: A three-switch mesh with IPv4 forwarding entries installed concurrently using AsyncP4RuntimeClient and asyncio.gather.
---

# Async concurrent multi-switch

Three switches form a full mesh. The example uses `AsyncP4RuntimeClient` and `asyncio.gather()` to populate their IPv4 LPM tables concurrently.

## Run it

From the repository root, start the topology.

```sh
sudo p4net examples/async_concurrent/topology.py
```

`setup(net)` connects to the three switches before starting the timer. The timer measures the batch of nine inserts until all calls return. Run `pingall` at the `p4net>` prompt.

```
p4net> pingall
Pinging 6 host pairs...
2/6 host pairs checked
4/6 host pairs checked
H \ H  h1   h2   h3
h1     -    1    1
h2     1    -    1
h3     1    1    -
6/6 succeeded
```

The matrix reports `6/6 succeeded`, covering both directions between each pair of hosts.

When finished, enter `exit` at the `p4net>` prompt to stop the topology.

## Topology

`examples/async_concurrent/topology.py`:

```python
--8<-- "examples/async_concurrent/topology.py"
```

Each host is on its own IPv4 subnet. `setup(net)` adds a `/32` on-link route and a static neighbor entry for each of the other two hosts. The switches forward the frames across the mesh using their IPv4 destination addresses.

## P4 program

`examples/async_concurrent/concurrent.p4`:

```p4
--8<-- "examples/async_concurrent/concurrent.p4"
```

## See also

- [Async client](../async-client.md) — overview, mastership patterns, cancellation semantics.
- [API Stability](../api-stability.md) — `AsyncP4RuntimeClient` is Stable in 1.x since 1.7.0.
