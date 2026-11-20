---
description: Proposed extensions for p4net, including PSA support, changes to running topologies, and async network orchestration.
---

# Roadmap

Discuss proposals in the [issue tracker](https://github.com/zhh2001/p4net/issues). For released changes, see the [changelog](changelog.md).

## Proposed extensions

These proposals still need design work and have no assigned release. Changes to public interfaces follow the [API stability policy](api-stability.md).

### PSA architecture support

p4net currently runs `v1model` programs on `simple_switch_grpc`. PSA support would require integrating a matching compiler and switch target, including startup, readiness checks, and pipeline installation.

The integration would also need to verify P4Info handling and test table, counter, register, and packet I/O operations on the new target.

### Live topology mutation

Adding or removing nodes and links while a network is running would require coordinated updates to the topology description, running nodes, Linux namespaces and interfaces, switch ports, and control-plane state. Failed changes would need to roll back, and resources created by successful changes would need to be cleaned up at shutdown.

To apply changes today, stop the network, edit the topology, and start a new `Network`. See [topology changes](known-limitations.md#4-live-topology-mutation-not-supported).

### AsyncNetwork

An `AsyncNetwork` could build on `Network.astart()` and `Network.astop()` to provide an async context manager or coordinate the lifecycle of multiple networks. See the [async lifecycle guide](async-client.md#shutting-down-a-network) for the current methods.

## Indefinitely deferred

- **Distributed experiments** would need remote resource management, links between machines, and coordinated setup and cleanup.
- **Web interface** for browser-based topology visualization and control.
