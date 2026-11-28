---
description: Proposed P4Net features for PSA programs, changes to running topologies, and async network management.
---

# Roadmap

These proposals have no scheduled release. Discuss them in [GitHub Issues](https://github.com/zhh2001/p4net/issues). See the [changelog](changelog.md) for released features and fixes.

## PSA architecture support

Run P4 programs that use the Portable Switch Architecture (PSA). See [P4 architecture support](known-limitations.md#3-psa-architecture-not-supported) for the current `v1model` support.

## Live topology changes

Add or remove nodes and links while the network is running. See [topology changes](known-limitations.md#4-live-topology-mutation-not-supported) for how to make changes today.

## Async network management

Manage a network with `async with`, or coordinate startup and shutdown for multiple networks in an async application. See the [async lifecycle guide](async-client.md#shutting-down-a-network) for the current methods.
