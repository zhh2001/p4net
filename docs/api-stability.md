---
description: Public API stability tiers, deprecation rules, and classifications for each p4net package.
---

# API stability

Stable APIs remain compatible throughout p4net 1.x. The tables below classify the exports in each package's `__all__`. A class's tier also applies to its documented constructor, methods, and properties unless a member has a separate entry. The top-level `p4net.Network` export is the same class as `p4net.network.Network`.

## Stability tiers

| Tier | Compatibility rule |
| --- | --- |
| Stable | Names, signatures, and documented behavior remain compatible in 1.x. Incompatible changes require the deprecation cycle and a new major version. |
| Provisional | Incompatible changes may appear in a minor release after the deprecation cycle. |
| Experimental | Interfaces may change without advance notice, including within 1.x. |
| Internal | No public compatibility commitment. This includes private members and implementation details not exported by a package. |

Bug fixes that restore documented behavior and compatible additions, such as keyword-only parameters with defaults, do not require a deprecation cycle.

## Deprecation policy

Deprecated stable and provisional APIs emit `DeprecationWarning` when called. They remain usable in at least two consecutive minor versions: the version that introduces the warning and the next minor version.

For example, an API deprecated in 1.2 remains available in 1.3. A provisional API may change incompatibly or be removed in 1.4 at the earliest. A stable API must also complete this cycle, but may only change incompatibly or be removed in the next major version. Experimental APIs do not require this cycle.

## Provisional tier and async client

`AsyncP4RuntimeClient`, `AsyncOperationCancelledError`, and `RunningSwitch.async_client` were introduced as provisional APIs in 1.6.0 and became stable in 1.7.0. They follow the same compatibility rules as other stable APIs.

The sync and async clients have different call patterns and some different methods and return types. For example, the sync client installs a pipeline with `set_pipeline_config()`, while the async client uses `await push_pipeline()`. See the [async client guide](async-client.md) for the complete comparison and event-loop requirements.

## Per-package classification

### `p4net.topo`

| Symbol | Tier | Scope |
| --- | --- | --- |
| `Host`, `P4Switch` | Stable | Documented data fields and their types. New fields may be added with compatible defaults. |
| `Link`, `LinkEndpoint` | Stable | Endpoint fields, MTU, symmetric impairments, per-direction overrides, and additive delay, jitter, and loss fields. |
| `Topology` | Stable | Node and link construction, lookups, validation, Graphviz export, and serialization. The `to_dict()` / `from_dict()` round-trip format is part of the contract. |
| `TopologyError` | Stable | Topology validation errors. |

### `p4net.network`

| Symbol | Tier | Scope |
| --- | --- | --- |
| `Network` | Stable | Lifecycle, host and switch access, `ping()`, `pingall()`, and `pingall6()`. In an event loop, use `await net.astart()` and `await net.astop()` to wait for startup and shutdown. Cancellation waits for cleanup. |
| `Network.xterm` | Experimental | Opens a host terminal through the available X display. |
| `Network.boot_timestamps` | Stable | Returns a new dictionary mapping switch names to BMv2 start times in Unix microseconds. Editing the dictionary does not change the network. Raises `NetworkNotRunningError` when the network is not running. |
| `RunningHost` | Stable | Host descriptor, namespace and interface access, addresses, command execution, and ping. |
| `RunningSwitch` | Stable | Switch descriptor, BMv2 process, sync client, compiler outputs, and log path. |
| `RunningSwitch.async_client` | Stable since 1.7 | Lazily creates an unconnected client with the sync client's current P4Info index. The instance is cached until `stop()` or `astop()` closes and clears it. |
| `RunningSwitch.boot_timestamp_us` | Stable | BMv2 start time in Unix microseconds. Raises `NetworkNotRunningError` if the underlying process has no boot timestamp. |
| `NetworkError`, `NetworkAlreadyRunningError`, `NetworkNotRunningError`, `NodeNotFoundError` | Stable | Network lifecycle and node lookup errors. |

### `p4net.control`

| Symbol | Tier | Scope |
| --- | --- | --- |
| `P4RuntimeClient` | Stable | Sync connection lifecycle, pipeline configuration, tables, counters, registers, multicast groups, and packet I/O. |
| `AsyncP4RuntimeClient` | Stable since 1.7 | Async connection lifecycle, pipeline installation, tables, counters, registers, and packet I/O. See the [async guide](async-client.md#differences-from-the-sync-api) for differences from the sync API. |
| `AsyncOperationCancelledError` | Stable since 1.7 | Async client cancellation error, subclassing `P4RuntimeError`. See the [async guide](async-client.md#errors-timeouts-and-cancellation) for the conditions that raise it. |
| `P4RuntimeClient.read_counter`, `P4RuntimeClient.reset_counter` | Stable | Read counter cells or reset their packet and byte counts to zero. |
| `P4RuntimeClient.read_register`, `P4RuntimeClient.write_register`, `AsyncP4RuntimeClient.read_register`, `AsyncP4RuntimeClient.write_register` | Stable | Read a register cell or array and write a cell through BMv2's Thrift service using `simple_switch_CLI`. |
| Automatic election ID generation | Provisional | `Network` and async clients without an explicit ID use millisecond timestamps. A directly constructed sync client defaults to `(1, 0)`. The explicit `(high, low)` parameter format is stable. |
| `P4InfoIndex` | Stable | P4Info loading, table/action/counter/register lookup, match and action encoding, and controller metadata helpers. The formatting method below has its own tier. |
| `P4InfoIndex.decode_match` | Provisional | Formats matches using P4Info field widths: IPv4 at 32 bits, MAC at 48 bits, IPv6 at 128 bits, and decimal values at other widths. |
| `CounterData` | Stable | `packet_count` and `byte_count`. |
| `RegisterSpec` | Stable | Immutable register metadata: `id`, `name`, `bitwidth`, and `size`. |
| `encode_value`, `encode_int`, `encode_ipv4`, `encode_mac`, `decode_int`, `decode_ipv4`, `decode_ipv6`, `decode_mac` | Stable | Value encodings and decodings. |
| `parse_lpm`, `parse_ternary`, `parse_range`, `format_exact`, `format_lpm`, `format_ternary`, `format_range`, `canonicalize` | Stable | Match parsing, formatting, and canonical byte representation. |
| `P4RuntimeError`, `ConnectionError`, `EncodingError`, `DuplicateEntryError`, `EntryNotFoundError`, `NoSuchTableError`, `NoSuchActionError`, `NoSuchFieldError`, `NoSuchRegisterError`, `NotPrimaryError`, `PipelineError` | Stable | Control-plane errors. |

### `p4net.runtime`

These primitives support direct management of Linux resources and BMv2 processes. `Network` combines them into a network lifecycle.

| Symbol | Tier | Scope |
| --- | --- | --- |
| `NetworkNamespace`, `VethPair`, `NSProcess` | Provisional | Namespace, interface, and process management. |
| `BMv2Switch` | Provisional | BMv2 startup, listener ownership checks, process status, and shutdown. |
| `apply_netem`, `clear_qdisc`, `disable_ipv6`, `enable_ipv6` | Provisional | Traffic shaping and interface IPv6 configuration. |
| `P4NetError`, `PrivilegeError`, `NamespaceError`, `LinkError`, `TcError`, `BMv2NotFoundError`, `BMv2StartupError` | Stable | Runtime errors and the common `P4NetError` base class. |

### `p4net.compiler`

| Symbol | Tier | Scope |
| --- | --- | --- |
| `P4Compiler` | Provisional | Compiler configuration and cache management. The compilation method has the stable contract below. |
| `P4Compiler.compile` | Stable | Compiles a P4 source with architecture, force-rebuild, and extra-argument options and returns a `CompileResult`. Cache layout is an implementation detail. |
| `CompileResult` | Stable | Compiler output metadata, including the `bmv2_json` and `p4info` paths. |
| `CompileError`, `CompilerNotFoundError` | Stable | Compiler rejection, missing compiler, and compiler version-check errors. Both inherit from `P4NetError`. |

### `p4net.cli`

| Interface | Tier | Scope |
| --- | --- | --- |
| Interactive commands | Stable | Command syntax and documented behavior in the [CLI reference](cli.md). |
| `p4net` console script | Stable | Topology file argument, `--no-shell`, `--unsafe`, `--log-dir`, `--pcap-dir`, `--extra-compile-arg`, and verbosity options. |
| `CommandDispatcher`, `P4NetShell` | Experimental | Python classes that implement command dispatch and the interactive shell. |
| `CLIError`, `CLIExit`, `CLIUsageError` | Stable | CLI errors and shell exit signaling. |

## Supported scope

p4net runs v1model programs on BMv2 on a single Linux machine. It supports both sync and async P4Runtime clients. PSA targets, changes to a running topology, and distributed multi-machine orchestration are outside the current scope. See [known limitations](known-limitations.md) and the [roadmap](roadmap.md).

## Logger namespace

The `p4net` logger and its `topo`, `network`, `control`, `runtime`, `compiler`, and `cli` subtrees are stable. Configure them with standard Python logging, for example `logging.getLogger("p4net.runtime")`.

Individual message text and the level of a specific event may change. See [Logging](logging.md) for configuration.

## Reporting incompatibilities

Compatibility regressions in stable APIs are bugs. Report them in the [issue tracker](https://github.com/zhh2001/p4net/issues) with the affected versions and a minimal reproducer.
