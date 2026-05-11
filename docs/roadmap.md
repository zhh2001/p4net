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

### Async P4Runtime client

An `aio-grpc`-based variant of `P4RuntimeClient` for callers building
event-driven controllers (e.g. those subscribing to many switches'
StreamChannels concurrently). Probably co-exists with the threaded
client rather than replacing it; same `P4InfoIndex` underneath. May
land in 1.x as a separate `AsyncP4RuntimeClient` if no breakage to
the existing client is required.

## Indefinitely deferred

- **Distributed simulation across multiple machines.** Federating two
  p4net instances needs L2 tunneling between hosts and a coordination
  layer; nothing in the current architecture is designed for it.
- **Web UI for topology visualization and live state.** Likely a
  separate companion repo if it ever happens. Keeps p4net itself
  framework-free.
