---
description: p4net's deployment and architecture limits, topology lifecycle, gRPC process constraints, and performance considerations.
---

# Known limitations

p4net runs P4 experiments with Linux network namespaces, veth interfaces, and BMv2. The following limits apply to its current interfaces. The [roadmap](roadmap.md) lists possible extensions.

## gRPC and worker processes { #1-grpc-connections-and-process-creation }

Both sync gRPC and `grpc.aio` use background threads. Forking a process with active gRPC resources can leave the child with locks held by threads that no longer exist there. Create worker processes first, then create and connect clients inside each worker. Async clients also need to run and close on the event loop where they connected.

For batch experiments, a parent that has not opened gRPC connections can start a fresh Python interpreter for each topology script:

```python
import subprocess
import sys

subprocess.run([sys.executable, "topology.py"], check=True)
```

Let each script close its network with `net.stop()` or, inside an event loop, `await net.astop()`. The p4net cleanup registry resets in forked children; that does not repair inherited gRPC resources. See [gRPC's fork documentation](https://github.com/grpc/grpc/blob/master/doc/fork_support.md) and the [async client guide](async-client.md).

## Local deployment { #2-single-host-operation-only }

`Network` creates namespaces, interfaces, and BMv2 processes on the local Linux system. It has no remote deployment or cross-machine coordination interface.

Multi-machine experiments need additional resource management and links between machines. Running separate `Network` instances alone does not provide that coordination.

## P4 architecture { #3-psa-architecture-not-supported }

`Network` uses the `v1model` architecture and `simple_switch_grpc`. Passing `arch="psa"` to `Topology.add_switch()` raises `TopologyError` while constructing the switch descriptor, before compilation.

PSA support would require integration with a matching compiler and switch target. BMv2 provides separate [target implementations](https://github.com/p4lang/behavioral-model/blob/main/targets/README.md); selecting a different architecture in a standalone compiler call does not add that integration to `Network`.

## Topology changes { #4-live-topology-mutation-not-supported }

`Topology` remains a mutable description. `Network.start()` creates resources from that description, but later calls to `Topology.add_host()` or `Topology.add_link()` do not create corresponding namespaces, interfaces, or running-node handles. `Network` has no interface for adding or removing running nodes and links.

To change the topology, stop the current network, edit the description, and create and start a new `Network`. Inside an event loop, await `net.astop()` before creating the replacement. Changes through lower-level runtime interfaces are managed separately from the network description.

## Software-switch performance { #5-bmv2-throughput-is-bounded }

BMv2 is intended for P4 development, testing, and debugging. Its throughput and latency depend on the P4 program, compiler and BMv2 builds, table contents, packet sizes and traffic patterns, and host resources. Logging, packet capture, and virtualization also affect the result. See [BMv2's performance notes](https://github.com/p4lang/behavioral-model/blob/main/docs/performance.md).

`Link.bandwidth` configures Linux `tc` traffic shaping. The achieved throughput also depends on packet processing and the traffic source. Configured link delay adds to switch processing and system scheduling time.

Report forwarding measurements with their topology, program, traffic, and environment. Network startup and shutdown time, end-to-end ping round-trip time, forwarding throughput, and per-switch processing latency are separate measurements.
