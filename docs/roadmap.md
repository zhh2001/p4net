---
description: p4net release milestones, candidate extensions, and the current behavior behind each proposal.
---

# Roadmap

## About this document

This page records released milestones and candidate extensions. Discuss designs
and release scope in the [issue tracker](https://github.com/zhh2001/p4net/issues)
or a pull request. Public interfaces follow the [API stability policy](api-stability.md).

## Released milestones

See the [changelog](changelog.md) for complete release notes.

| Version | Date | Main changes |
| --- | --- | --- |
| 0.1.0 | 2026-05-10 | Topology DSL, P4Runtime control, BMv2 orchestration, and interactive CLI. |
| 0.2.0 | 2026-05-10 | Controller packet I/O, IPv6 host addresses, asymmetric link impairments, Graphviz export, and host terminals. |
| 1.0.0 | 2026-05-10 | Public API stability policy, expanded CI matrix, wheel installation checks, and security documentation. |
| 1.1.0 | 2026-05-11 | INT example, logging configuration, and additive per-direction link impairments. |
| 1.2.0 | 2026-05-11 | Register reads and writes, with register-based switch IDs in the INT example. |
| 1.3.0 | 2026-05-11 | Multi-hop INT example using the same P4 program and distinct register values on each switch. |
| 1.4.0 | 2026-05-11 | `RunningSwitch.boot_timestamp_us` and timestamp alignment in the multi-hop INT example. |
| 1.5.0 | 2026-05-11 | `Network.boot_timestamps` and a configurable INT coordination-file path for separate experiments. |
| 1.5.1 | 2026-05-12 | Revised multi-hop INT integration assertions. |
| 1.6.0 | 2026-05-14 | Provisional async P4Runtime client, lazy switch client access, and the async concurrent example. |
| 1.7.0 | 2026-05-14 | `AsyncP4RuntimeClient`, `AsyncOperationCancelledError`, and `RunningSwitch.async_client` became stable. |

## 1.x candidates (additive, no API breakage)

This page does not assign a new feature to a specific 1.x release. Compatible
fixes and additions follow the API stability policy.

## 2.0 candidates (may require API breakage)

These directions need design work before their interfaces and release scope
can be decided. Adding a separate method or class can preserve compatibility.
An incompatible change to a stable API must complete the deprecation cycle and
ship in a new major version.

### PSA architecture support

`Topology.add_switch()` already has an `arch` parameter, currently restricted
to `"v1model"`. `Network` launches `simple_switch_grpc`. PSA support needs
integration with a matching compiler and runtime target. BMv2 documents its
separate [target implementations](https://github.com/p4lang/behavioral-model/blob/main/targets/README.md).

Work includes target selection, startup arguments and readiness checks, pipeline
installation, and tests for table, counter, register, and packet I/O operations.
`P4InfoIndex` and controller metadata handling need validation against the new
target's P4Info. The design must determine which existing interfaces can be
reused and whether any public interface needs to change.

### Live topology mutation

`Topology` remains a mutable description after `Network.start()`. Adding a host
or link to that description does not create running resources. `Network` has
no live node or link add/remove interface.

Live operations need to keep the description, running-node handles, namespaces,
interfaces, port assignments, and control-plane state consistent. The design
also needs rollback when an operation fails and cleanup when the network stops.
The current workflow is to stop the network, edit its description, and create
and start a new `Network`. See [topology changes](known-limitations.md#4-live-topology-mutation-not-supported).

### AsyncNetwork

`AsyncP4RuntimeClient` supports concurrent RPC operations, and
`await net.astop()` already waits for network shutdown. `Network.start()` is
synchronous and starts BMv2 switches sequentially, waiting for each switch to
be ready before starting the next.

In a coroutine, use `await asyncio.to_thread(net.start)` for startup and
`await net.astop()` for shutdown. Keep async clients' event loops running until
cleanup finishes. See the [async lifecycle guide](async-client.md#shutting-down-a-network).

A future async startup interface or `AsyncNetwork` could build on the existing
shutdown API to coordinate startup, cancellation, and rollback within an event
loop. An async design can retain the sync lifecycle while adding separate
entry points.

## Indefinitely deferred

- **Distributed experiments.** `Network` manages resources on one Linux machine.
  Multi-machine orchestration needs remote resource management, links between
  hosts, and coordination of setup and cleanup.
- **Web interface.** The current interfaces provide an interactive CLI and
  Graphviz topology export. Browser-based visualization and control are outside
  the candidate work listed above.
