---
description: Diagnose startup, P4Runtime and connectivity failures using logs, host state and switch configuration.
---

# Troubleshooting

The examples below use `topology.py`, hosts `h1` and `h2`, and switch `s1`. Substitute the names from your topology.

## Collect logs and current state

Activate the Python environment where p4net is installed, then start the topology with Python debug logging, BMv2 logs and packet captures:

```bash
sudo env "PATH=$PATH" "$(command -v p4net)" -vv \
  --log-dir ./logs --pcap-dir ./pcaps topology.py
```

If the shell opens, check the network and switch state:

```text
p4net> status
p4net> switches
p4net> s1 log
```

`switches` shows each switch's gRPC address, PID and log path. `s1 log` prints the path, not the file contents. Read the log from another terminal:

```bash
tail -n 80 ./logs/s1.log
```

If startup fails before the shell opens, read the exception and the log path it reports. A compiler error contains p4c's stderr; a switch log becomes available once BMv2 is launched.

## Permission denied when creating namespaces or links

If namespace or link creation reports `Operation not permitted` or `Permission denied`, run the topology with the `sudo` command above. Installing p4net and compiling P4 programs can use your regular account.

A failed `sysctl` write is reported as `NamespaceError`. Read the key and command error in its message to check for insufficient permissions, a missing interface or an unavailable `sysctl` command.

## External commands cannot be found

`CompilerNotFoundError` identifies a missing or unusable p4c. `BMv2NotFoundError` identifies a missing `simple_switch_grpc`. Host commands and namespace configuration also depend on the Linux network tools:

```bash
command -v p4c simple_switch_grpc ip tc ping sysctl
```

Install missing tools using [Installation](getting-started/installation.md#required-external-tools). If a tool is installed in a custom directory, include that directory in `PATH` when invoking `sudo`, as in the startup command above. Register operations also need `simple_switch_CLI` and BMv2's Thrift service.

## BMv2 exits or its gRPC port does not become ready

`BMv2StartupError` reports an early process exit (`exited before becoming ready`), a port owned by another process (`already in use by another process`), or a timeout (`did not open its gRPC and Thrift ports within 10.0s`). Check the reported switch log for the cause, such as an invalid BMv2 argument, an interface error or a port conflict.

BMv2 uses both a gRPC port and a Thrift port. For the first switch, the defaults are 50051 and 9090; subsequent switches increment each port by one. Check both listeners:

```bash
sudo ss -ltnp 'sport = :50051'
sudo ss -ltnp 'sport = :9090'
```

Compare the listener PID with the switch PID from `switches`. BMv2 may continue running its Thrift service after a gRPC bind failure, so check the log for `Address already in use` even while the process is alive.

Match the PID to the experiment that owns it, then stop that experiment or choose other ports. Set `grpc_port` and `thrift_port` in `add_switch()` to select ports for a new topology. p4net assigns default ports by switch order and checks listener ownership during startup. An occupied port causes startup to fail; select other ports explicitly.

For a switch that takes longer to start, the lower-level `BMv2Switch` interface accepts `startup_timeout` or `wait_until_ready(timeout=...)`. `Network` uses the default 10-second wait.

## P4Runtime connection or write failures

Startup checks that the new BMv2 process owns both listeners. The P4Runtime client must also complete arbitration with the correct `device_id` and role. For `ConnectionError` or gRPC `UNAVAILABLE`, check the address from `switches`, the BMv2 process and its log, then reconnect after resolving the cause.

p4net reports control failures through exceptions from `p4net.control.exceptions`:

| Exception | What to check |
| --- | --- |
| `NotPrimaryError` | Controller election ID, role permissions and other connected controllers |
| `PipelineError` | Whether the BMv2 JSON and P4Info describe the same compiled program, and the switch's rejection message |
| `DuplicateEntryError` | Whether the entry already exists before inserting it |
| `EntryNotFoundError` | Whether the entry exists before modifying or deleting it |
| `P4RuntimeError` | The error message and operation-specific preconditions |

For read or write `FAILED_PRECONDITION` errors, check that the switch has a forwarding pipeline. A standalone synchronous client loads one with `set_pipeline_config()`; the asynchronous client uses `await push_pipeline()`. `Network.start()` performs this setup for its managed clients.

P4Runtime can wrap per-entry failures in an outer gRPC `UNKNOWN` status. Both clients use the first recognizable failure code in request order to choose the exception type. If the details cannot be decoded, the result remains a generic `P4RuntimeError`. Print the exception type and message first. For the original RPC details, inspect `exc.__cause__`; when it is a `grpc.RpcError`, use its `code()`, `details()` and `trailing_metadata()` methods.

See the [async client guide](async-client.md) for asynchronous connection and cancellation behavior.

## `pingall` reports failures

Check host addresses, routes, neighbor resolution and P4 forwarding in that order:

```text
p4net> h1 ifconfig
p4net> h1 cmd ip route
p4net> h1 cmd ip neigh show
p4net> s1 table list
```

`h1 ifconfig` runs `ip -br addr` inside the host namespace. Neighbor entries in `INCOMPLETE` or `FAILED` state indicate unresolved addresses. After listing the switch's tables, inspect the relevant entries with `s1 table dump` and the table name.

ARP behavior depends on the P4 program. The [Quick Start](getting-started/quickstart.md) program forwards Ethernet frames between two ports, including ARP, so hosts learn dynamic neighbors. A program that only forwards IPv4 may need static neighbors or a controller that handles ARP.

For the same-subnet addresses and MACs used in Quick Start, static neighbors can be configured in both directions:

```text
p4net> h1 cmd ip neigh replace 10.0.0.2 lladdr 00:00:00:00:00:02 dev h1-eth0 nud permanent
p4net> h2 cmd ip neigh replace 10.0.0.1 lladdr 00:00:00:00:00:01 dev h2-eth0 nud permanent
p4net> pingall
```

Static neighbors provide IP-to-MAC mappings. Forwarding entries still need to match the P4 program.

## IPv6 addresses or connectivity are unexpected

Check the topology's `ip6`, `ip6_a` and `ip6_b` settings, then inspect the host interface:

```text
p4net> h1 cmd ip -6 addr show dev h1-eth0
p4net> h1 cmd sysctl net.ipv6.conf.h1-eth0.disable_ipv6
```

p4net enables IPv6 on host interfaces with configured IPv6 addresses and disables Router Advertisement processing and SLAAC there. Interfaces without IPv6 configuration have IPv6 disabled. IPv6 connectivity needs neighbor resolution and IPv6 forwarding. Forward Neighbor Discovery packets through P4, handle them in a controller, or configure static IPv6 neighbors. See the [dual-stack example](examples/dual-stack.md).

## A namespace or interface already exists

Stop the original experiment before starting another topology with the same node names. From Python, call `net.stop()`; in asynchronous code, use `await net.astop()` to wait for cleanup. A process terminated with `SIGKILL` cannot run Python cleanup handlers.

Inspect any remaining resources:

```bash
sudo ip netns list
sudo ip -br link
sudo ip netns exec h1 ip -br link
```

If `h1` and the root-namespace interface `s1-eth1` are confirmed leftovers from the stopped experiment, remove the switch end of the veth pair, then the namespace:

```bash
sudo ip link delete s1-eth1
sudo ip netns delete h1
```

The host end, `h1-eth0`, normally lives inside `h1`. Deleting either end of a veth pair removes both ends.

## P4 edits do not appear in the running network

Check the `p4_src` path in `add_switch()` and restart the network after editing the program. The compiler checks preprocessed input, including included headers, before reusing cached output. An already running switch keeps its loaded pipeline until it is updated or restarted.

The compiler cache directory must be writable. To select a cache in the current directory and force compilation:

```python
from pathlib import Path

from p4net.compiler import P4Compiler

compiler = P4Compiler(cache_dir=Path(".cache/p4net"))
result = compiler.compile(Path("port_swap.p4"), force=True)
print(result.bmv2_json)
print(result.p4info)
```

## `xterm` cannot open a window

The host's `xterm` command needs an installed `xterm`, a `DISPLAY` value and access to the X server from its network namespace. Check the environment used to launch p4net:

```bash
command -v xterm
printenv DISPLAY
```

If `sudo` removes the display environment, preserve `DISPLAY` and the applicable X authorization settings when launching p4net. The server must also authorize that process. To run a command without a graphical window, use the CLI's `h1 cmd` command, for example `h1 cmd ip -br addr`.

## Graphviz rendering cannot find `dot`

Install Graphviz with `sudo apt install graphviz` to render SVG, PNG or PDF. DOT export works without `dot`:

```text
p4net> topology graph ./topology.dot format=dot
p4net> topology graph ./topology.svg format=svg
```

The second command requires Graphviz. `topology graph` without a path prints the DOT source in the shell.

## Report a problem

Include the version outputs below, the complete exception, a minimal topology and P4 program, and relevant lines from the switch log when [opening an issue](https://github.com/zhh2001/p4net/issues):

```bash
python -c "import p4net; print(p4net.__version__)"
p4c --version
simple_switch_grpc --version
```
