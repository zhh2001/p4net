---
description: Configure P4Net logging in the CLI and in Python scripts.
---

# Logging

P4Net uses Python's standard `logging` module. Its loggers are grouped under `p4net` so you can configure the whole library or one subsystem.

## Logger names

The `p4net` logger and the `p4net.topo`, `p4net.network`, `p4net.control`, `p4net.runtime`, `p4net.compiler`, and `p4net.cli` subtrees are stable in 1.x. Use these names for filters and level settings you want to keep across releases. Individual module names, message text, and the level of a specific event may change. See the [API stability policy](api-stability.md#logger-namespace).

## CLI verbosity

| Option | Minimum Python log level |
| --- | --- |
| Default | `WARNING` |
| `-v` or `--verbose` | `INFO` |
| `-vv` or more | `DEBUG` |

Use `-v` to log network startup, shutdown, and P4Runtime connections. Use `-vv` to include details such as compiler cache hits and misses. Run this example from the repository root.

```sh
sudo p4net -v examples/quick_start/quick_start.py
```

In interactive mode, startup and shutdown notices such as `Ready` and `Stopping topology...` appear at every log level. Use `--log-dir DIR` to choose where BMv2 writes its own log files.

## Programmatic configuration

Configure Python logging before starting the network. This example uses `INFO` as the default and enables `DEBUG` for the runtime subtree.

```python
import logging

logging.basicConfig(level=logging.INFO)

logging.getLogger("p4net.runtime").setLevel(logging.DEBUG)
```

If your application already configures logging, set the logger level in that configuration and allow `DEBUG` on the relevant handler.
