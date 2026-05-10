---
description: Released milestones and forward-looking v0.3.0 candidates for p4net.
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

## v0.3.0 candidates

### PSA architecture support

Beyond v1model. Requires `p4c` and BMv2 with the PSA target compiled
in (not standard in many distributions). The compiler wrapper would
need to learn how to select the architecture and the `simple_switch`
process variant; `P4InfoIndex` should keep working unchanged because
P4Runtime isolates the architecture from the controller.

### Live topology mutation

`Network.add_host(...)`, `Network.add_link(...)`, and their `remove_*`
counterparts while the network is already running. Currently the
`Topology` is frozen at `start()` time. The realisation primitives
(namespaces, veth pairs) already support live add/remove; the
orchestrator's bookkeeping just needs to re-enter the same paths.

### Async P4Runtime client

An `aio-grpc`-based variant of `P4RuntimeClient` for callers building
event-driven controllers (e.g. those subscribing to many switches'
StreamChannels concurrently). Probably co-exists with the threaded
client rather than replacing it; same `P4InfoIndex` underneath.

### Symmetric base + per-direction extra link impairment

v0.2 ships either symmetric or asymmetric link parameters (rejecting
the mix at construction time). A `delay_a_to_b_extra` /
`delay_b_to_a_extra` pattern would let users layer a per-direction
adjustment on top of a symmetric base — e.g. a baseline 10 ms link
with an additional 50 ms only on the uplink — without forcing them to
spell out both sides explicitly. Same shape for the other three
parameters (`bandwidth`, `jitter`, `loss_pct`). Carried forward from
phase-12 OQ #4.

### `<switch> table dump` cross-switch action-param disambiguation

Today `_render_action_params` walks the index of whatever switch the
dispatcher dispatched against. In a multi-switch topology where two
switches happen to share an action name with different parameter
widths, the per-switch lookup is already correct — but earlier
prototypes had a "first switch wins" path that could leak in via
direct module-level helpers. Tighten the contract by pinning every
helper to a specific `P4InfoIndex` rather than the first one in the
network. Carried forward from phase-13 OQ #1.

## Indefinitely deferred

- **Distributed simulation across multiple machines.** A v1.0
  conversation; out of scope for v0.x. Federating two p4net instances
  needs L2 tunneling between hosts and a coordination layer; nothing
  in the current architecture is designed for it.
- **Web UI for topology visualization and live state.** Likely a
  separate companion repo if it ever happens. Keeps p4net itself
  framework-free.
