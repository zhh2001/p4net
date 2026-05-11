---
description: How p4net uses Python's logging module — namespace hierarchy and level conventions.
---

# Logging

p4net uses the standard-library `logging` module under a `p4net` root
logger. Every internal module logs under its dotted path — e.g.
`p4net.network.orchestrator`, `p4net.runtime.bmv2`,
`p4net.compiler.p4c` — so consumers can filter or redirect any
subsystem independently.

## Namespace

The `p4net.*` logger namespace is **stable**: names will not change
in 1.x. Concretely:

- `logging.getLogger("p4net.network").setLevel(logging.DEBUG)` works
  in 1.0 and will keep working through every 1.x release.
- New submodule names may be added (e.g.
  `p4net.network.cleanup`), but existing ones won't be renamed or
  removed without a major version bump.

## Levels

| Level   | What you'll see |
| ------- | --------------- |
| ERROR   | Failures the library couldn't handle. Usually paired with a raised exception. |
| WARNING | Unexpected but handled. The library recovered or worked around something. |
| INFO    | Lifecycle events: `Network.start` / `stop`, BMv2 spawn, gRPC client connect, primary acquired. |
| DEBUG   | Detailed decisions: compiler cache hit/miss, retry attempts, election ID, low-level netlink calls. |

Log **levels** for specific events are **not** part of the stable
contract — we may downgrade INFO events to DEBUG if they prove too
noisy in real usage, or vice versa. The level taxonomy itself is
stable; which-event-uses-which is not.

Log **message text** is **not** stable. Don't grep production logs
for specific strings; use the level and logger name instead.

## CLI verbosity

The `p4net` console script accepts:

- no flag → default level `WARNING` (only problems show up)
- `-v` / `--verbose` → `INFO`
- `-vv` → `DEBUG`

Higher counts (`-vvv`, …) also resolve to `DEBUG`; there is no level
quieter than the standard `WARNING`.

## Programmatic configuration

In your own scripts, configure `logging` before importing or
instantiating p4net components — basicConfig only takes effect on
the first call:

```python
import logging

logging.basicConfig(level=logging.INFO)

# Optional: turn one subsystem up to DEBUG while keeping the rest at INFO.
logging.getLogger("p4net.runtime").setLevel(logging.DEBUG)
```

For more elaborate configuration (file handlers, JSON formatters,
forwarding to syslog), build a `logging.config.dictConfig` payload
the same way you would for any other library.
