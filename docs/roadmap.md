---
description: Released milestones and forward-looking 2.0 candidates for p4net.
---

# Roadmap

## About this document

This is a living roadmap, not a commitment. Items are listed in rough
priority order within each release; relative ordering can change as
issues are filed and trade-offs become clearer. PRs for any item are
welcome — please comment on (or open) the linked issue first so that
design discussion happens once, in public, before the code lands.

## Released milestones

- **v0.1.0** (2026-05-10) — initial public release. Topology DSL,
  P4Runtime control plane, BMv2 orchestration, interactive CLI. See
  [Changelog](changelog.md).
- **v0.2.0** (2026-05-10) — controller packet I/O, IPv6 host addressing,
  asymmetric link impairment, topology visualizer, `xterm` helper. See
  [Changelog](changelog.md).
- **v1.0.0** (2026-05-10) — public API stability commitment, OQ
  backlog cleanup (phase-13 #1/#2/#4, phase-16 #1), CI hardening
  (Ubuntu 22.04 + 24.04 matrix, wheel-install smoke job), expanded
  SECURITY.md, performance baseline, Unicode-aware doc anchors. See
  [API Stability](api-stability.md), [Performance](performance.md),
  and [Changelog](changelog.md).
- **v1.1.0** (2026-05-11) — polish minor: INT example, unified
  `p4net.*` logger hierarchy with CLI verbosity control, per-direction
  additive link impairment, known-limitations catalogue, custom 404
  page. See [Logging](logging.md),
  [Known Limitations](known-limitations.md), and
  [Changelog](changelog.md).
- **v1.2.0** (2026-05-11) — register API
  (`P4RuntimeClient.read_register` / `write_register`), INT example now
  uses a register-backed `switch_id` instead of a hard-coded constant,
  INT shim timestamp field correctly named `ingress_timestamp_us`,
  asymmetric-delay test tolerance widened to eliminate suite-load
  flakes. See [API Stability](api-stability.md) and
  [Changelog](changelog.md).
- **v1.3.0** (2026-05-11) — multi-hop INT example: two switches in a
  linear path each insert their own metadata shim; the receiver
  decodes the full hop-by-hop stack. Same P4 binary on both switches;
  per-switch identity supplied via the v1.2 register API. No public
  API changes. See [Changelog](changelog.md).
- **v1.4.0** (2026-05-11) — cross-switch INT timestamp alignment via
  `RunningSwitch.boot_timestamp_us` (wall-clock μs since Unix epoch at
  BMv2 process start). The multi-hop INT example now writes a
  coordination file at start; the listener displays aligned wall-clock
  timestamps and per-hop forwarding latency. See [Changelog](changelog.md).
- **v1.5.0** (2026-05-11) — `Network.boot_timestamps` helper (dict of
  all switches' boot times) and env-var-keyed coordination file
  (`P4NET_INT_BOOT_TIMES_PATH`) for the multi-hop INT example so
  concurrent topologies don't collide. See [Changelog](changelog.md).
- **v1.5.1** (2026-05-12) — drop misdesigned aligned-causal assertion
  in the multi-hop INT integration test. See [Changelog](changelog.md).
- **v1.6.0** (2026-05-14) — `AsyncP4RuntimeClient` parallel to the
  sync client; `RunningSwitch.async_client` lazy property; example
  `async_concurrent/` showing `asyncio.gather` across switches. New
  symbols are Provisional in 1.x with a commitment to upgrade to
  Stable within two minor releases. See
  [Async client](async-client.md) and [Changelog](changelog.md).
- **v1.7.0** (2026-05-14) — `AsyncP4RuntimeClient`,
  `AsyncOperationCancelledError`, and `RunningSwitch.async_client`
  promoted from Provisional to **Stable** following real-world user
  soak. No source-level API change. See [Changelog](changelog.md).

## 1.x candidates (additive, no API breakage)

None currently open; ideas welcome via issues.

## 2.0 candidates (may require API breakage)

### PSA architecture support

Beyond v1model. Requires `p4c` and BMv2 with the PSA target compiled
in (not standard in many distributions). The compiler wrapper would
need to learn how to select the architecture and the `simple_switch`
process variant; `P4InfoIndex` should keep working unchanged because
P4Runtime isolates the architecture from the controller. Promoted
to 2.0 because the `Topology.add_switch` signature may need a new
required argument or a redesigned default.

### Live topology mutation

`Network.add_host(...)`, `Network.add_link(...)`, and their `remove_*`
counterparts while the network is already running. Currently the
`Topology` is frozen at `start()` time. The realisation primitives
(namespaces, veth pairs) already support live add/remove; the
orchestrator's bookkeeping just needs to re-enter the same paths.
Promoted to 2.0 because the `Topology` immutability invariant is
documented in 1.x as part of the stable API.

### AsyncNetwork

`AsyncP4RuntimeClient` shipped in v1.6 closes the per-RPC story, but
`Network.start()` itself is still synchronous and uses threads to
parallelise BMv2 startup. A future `AsyncNetwork` (or
`Network.start_async()`) would make the whole lifecycle composable
inside an existing event loop without `asyncio.to_thread`
wrappers. Out of scope for 1.x because adding an `async def start`
alongside the sync `start` would either require keyword-only
disambiguation or split into a new class; cleanest design needs the
2.0 freedom to rethink lifecycle.

## Indefinitely deferred

- **Distributed simulation across multiple machines.** Federating two
  p4net instances needs L2 tunneling between hosts and a coordination
  layer; nothing in the current architecture is designed for it.
- **Web UI for topology visualization and live state.** Likely a
  separate companion repo if it ever happens. Keeps p4net itself
  framework-free.
