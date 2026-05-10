---
description: API stability classification per public symbol — what 1.0 means, deprecation policy, and per-package tier tables.
---

# API stability

p4net's 1.x line commits to a stable public API. This page defines
what "stable" means in practice, classifies every public symbol by
stability tier, and documents the deprecation cycle.

## What "1.0" means for p4net

The 1.x line will not introduce breaking changes to **stable** APIs
without a deprecation cycle. Some APIs are explicitly marked
**provisional** — they may evolve through a deprecation cycle within
1.x. Some APIs are explicitly marked **experimental** — they may
change without warning within 1.x and should not be relied on for
anything you can't easily refactor. Anything not exported in
`__all__` is **internal** regardless of name; do not import it.

## Stability tiers

**Stable.** Backwards-compatible across the entire 1.x line. Behavior
changes only in 2.0+. Bug-fix changes that preserve the documented
contract are always permitted.

**Provisional.** May change in a 1.x minor release, but only after a
deprecation warning has been visible for at least two consecutive
minor versions. New keyword-only arguments may be added without a
deprecation cycle (default-valued additions are not breaking).

**Experimental.** May change without notice within 1.x. Do not rely
on the precise interface; pin to a specific version if you need
reproducibility.

**Internal.** Not part of the public API regardless of language
semantics. Do not import directly. Anything not exported in a
package's `__all__` is internal even when nominally accessible.

## Deprecation policy

- Deprecated APIs emit a `DeprecationWarning` from the call site.
- Deprecated APIs survive at least two consecutive minor releases
  (e.g. deprecated in 1.2 → still works in 1.3 → removed earliest
  in 2.0; or earlier than 2.0 only for **provisional** /
  **experimental** APIs after the deprecation cycle has elapsed).
- Removal in a minor release is permitted ONLY for provisional and
  experimental APIs after the cycle.
- Stable APIs can only be removed in a major release (2.0+), and
  only after one full minor cycle of deprecation in the prior major.

## Per-package classification

### `p4net.topo`

The user-facing topology DSL. Mostly stable.

| Symbol | Tier | Note |
| --- | --- | --- |
| `Host` | Stable | Field names and types are stable. New fields may be added with safe defaults. |
| `P4Switch` | Stable | Same as `Host`. |
| `Link` | Stable | Symmetric and asymmetric impairment fields (`bandwidth`, `delay`, `jitter`, `loss_pct` and their `*_a_to_b` / `*_b_to_a` variants) are stable. |
| `LinkEndpoint` | Stable | |
| `Topology` | Stable | `add_host`, `add_switch`, `add_link`, `validate`, `to_graphviz`, `render_graphviz`, `to_dict`, `from_dict` are stable. The `to_dict` / `from_dict` round-trip format is part of the public contract. |
| `TopologyError` | Stable | |

### `p4net.network`

Stable user-facing surface; some lower-level details provisional.

| Symbol | Tier | Note |
| --- | --- | --- |
| `Network` | Stable | `start`, `stop`, `host`, `switch`, `pingall`, `pingall6`, `__enter__`, `__exit__` are stable. |
| `Network.xterm` | Experimental | Depends on `$DISPLAY`; behavior may evolve as we refine the spawned-process tracking. |
| `RunningHost` | Stable | `name`, `primary_ip`, `primary_ip6`, `interfaces`, `interfaces6`, `ping`, `exec`, `popen` are stable. |
| `RunningSwitch` | Stable | `name`, `client`, `bmv2`, `compile_result`, `log_file`, `descriptor` are stable. |
| `NetworkError` and subclasses | Stable | `NetworkAlreadyRunningError`, `NetworkNotRunningError`, `NodeNotFoundError`. |

### `p4net.control`

Stable client API; provisional codec edge cases.

| Symbol | Tier | Note |
| --- | --- | --- |
| `P4RuntimeClient` | Stable | `connect`, `disconnect`, `set_pipeline_config`, `get_pipeline_config`, `insert_table_entry`, `modify_table_entry`, `delete_table_entry`, `list_table_entries`, `clear_table`, `read_counter`, `write_counter`, `add_multicast_group`, `modify_multicast_group`, `delete_multicast_group`, `list_multicast_groups`, `send_packet_out`, `on_packet_in`, `expect_packet_in`. |
| `P4RuntimeClient` election ID | Provisional | The millisecond-time-since-epoch generator is stable behavior; the underlying type (`tuple[int, int]`) is stable; the strategy may change if it ever causes operational pain. |
| `P4InfoIndex` | Stable | Top-level lookups (`tables`, `actions`, `counters`, controller-packet metadata) are stable. |
| `P4InfoIndex.decode_match` | Provisional | Width-aware formatting rules (32-bit → IPv4, 48-bit → MAC, 128-bit → IPv6, else decimal) are stable in 1.x; we may add new field-type rules with deprecation notice. |
| `CounterData` | Stable | |
| Codec helpers (`encode_value`, `encode_int`, `encode_ipv4`, `encode_mac`, `decode_int`, `decode_ipv4`, `decode_ipv6`, `decode_mac`, `parse_lpm`, `parse_ternary`, `parse_range`, `format_exact`, `format_lpm`, `format_ternary`, `format_range`, `canonicalize`) | Stable | The string formats they emit and consume are part of the public contract. |
| Exception types (`P4RuntimeError`, `ConnectionError`, `EncodingError`, `DuplicateEntryError`, `EntryNotFoundError`, `NoSuchTableError`, `NoSuchActionError`, `NoSuchFieldError`, `NotPrimaryError`, `PipelineError`) | Stable | |

### `p4net.runtime`

Provisional. These are intentionally low-level escape hatches; the
higher-level `Network` orchestrator covers most use cases.

| Symbol | Tier | Note |
| --- | --- | --- |
| `NetworkNamespace` | Provisional | |
| `VethPair` | Provisional | |
| `BMv2Switch` | Provisional | |
| `NSProcess` | Provisional | |
| `apply_netem`, `clear_qdisc` | Provisional | |
| `disable_ipv6`, `enable_ipv6` | Provisional | |
| Exception types (`LinkError`, `NamespaceError`, `BMv2NotFoundError`, `BMv2StartupError`, `TcError`, `PrivilegeError`, `P4NetError`) | Stable | |

### `p4net.compiler`

Provisional internals; stable compiler interface.

| Symbol | Tier | Note |
| --- | --- | --- |
| `P4Compiler` | Provisional | The `compile()` method's behavior is stable; the cache directory layout under `~/.cache/p4net/compiler/` is provisional. Users should never reach into the cache directly. |
| `CompileResult` | Stable | `bmv2_json` and `p4info` Path attributes are part of the public contract. |
| `CompilerError` | Stable | |

### `p4net.cli`

The CLI command grammar (what users type at the prompt) is **stable**
as a user-facing contract — every command in the
[CLI Reference](cli.md) will work the same way across 1.x. The
Python class structure used to implement it is **experimental**;
external code should not subclass `CommandDispatcher` or `P4NetShell`.

| Symbol | Tier | Note |
| --- | --- | --- |
| The CLI command grammar | Stable | Documented in [CLI Reference](cli.md). |
| `CommandDispatcher` (Python class) | Experimental | Implementation detail; subclassing not supported. |
| `P4NetShell` (Python class) | Experimental | Implementation detail; subclassing not supported. |
| `CLIError`, `CLIExit`, `CLIUsageError` | Stable | |
| `p4net` console script (argv contract) | Stable | `--no-shell`, `--unsafe`, `--log-dir`, `--pcap-dir`, `--extra-compile-arg`. |

## What's explicitly NOT supported in 1.x

- **PSA architecture.** Only v1model is supported. PSA support is
  planned for 2.0; track in [Roadmap](roadmap.md).
- **Live topology mutation.** Build a `Topology`, hand it to
  `Network`, do work, then `stop`. Adding or removing hosts,
  switches, or links at runtime is not supported in 1.x; planned
  for 2.0.
- **Async P4Runtime client.** The client is synchronous (blocking
  gRPC). An asyncio + `grpc.aio` variant is post-1.x.
- **Distributed simulation across multiple machines.** Single-host
  only.

## Reporting incompatibilities

We treat compatibility regressions in stable APIs as bugs to fix in
a patch release. If a 1.x version breaks code that worked on a
prior 1.x version using only stable APIs, please file an issue at
<https://github.com/zhh2001/p4net/issues> with a minimal reproducer.
