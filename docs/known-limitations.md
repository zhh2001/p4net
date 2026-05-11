---
description: What p4net does not support in the 1.x line, with workarounds where they exist.
---

# Known limitations

The 1.x line of p4net intentionally scopes out several capabilities, either
because they require breaking changes (planned for 2.0+) or because they're
inherent to the underlying BMv2 software dataplane. This page enumerates
them so users encounter the boundary before hitting it in their own code.

## 1. Single `Network` lifecycle per Python process

- **Symptom**: starting a second `Network` — or starting / stopping /
  starting in a loop — inside one Python process eventually deadlocks
  during a `subprocess.run` or `Popen` call.
- **Why**: gRPC's `StreamChannel` spawns background threads. When
  `subprocess.run` then forks, the multi-threaded-fork-in-Python pathology
  surfaces and the child can hang holding a stale lock.
- **Workaround**: invoke a fresh Python process per topology run, e.g.
  `subprocess.run([sys.executable, "topology.py"])` from your harness.
  The `docs/performance.md` measurement script uses this pattern.
- **Status**: not planned for 1.x. An async P4Runtime client (a 2.0
  candidate) would address this structurally.

## 2. Single-host operation only

- **Symptom**: `Network` only spawns hosts, switches and links on the
  current machine.
- **Why**: design choice. Cross-host coordination needs a distributed
  control plane and L2 tunneling, neither of which is in scope.
- **Workaround**: none within p4net 1.x. For multi-host topologies, run
  separate `Network` instances per machine and bridge externally with the
  tunnel of your choice.
- **Status**: 2.0 candidate.

## 3. PSA architecture not supported

- **Symptom**: P4 programs targeting `psa.p4` fail compilation through
  `P4Compiler`, or produce a `P4Info` the runtime can't drive.
- **Why**: parser / deparser conventions, metadata layout, and the `P4Info`
  schema differ from v1model. Full support requires architecture-aware
  tooling on the compiler wrapper and possibly a different switch binary.
- **Workaround**: use v1model. Almost all P4 tutorials and educational
  pipelines target v1model anyway.
- **Status**: 2.0 candidate.

## 4. Live topology mutation not supported

- **Symptom**: calling `topology.add_host(...)` or `topology.add_link(...)`
  after `network.start()` has no effect on the running network.
- **Why**: BMv2 doesn't support dynamic port reconfiguration cleanly, and
  veth pair changes mid-run would race with running P4 pipelines.
- **Workaround**: stop the `Network`, modify the `Topology`, start a new
  `Network`.
- **Status**: 2.0 candidate.

## 5. BMv2 throughput is bounded

- **Symptom**: aggregate throughput across a topology rarely exceeds 1–10
  Mbps; latency typically 1–10 ms per hop.
- **Why**: BMv2 is a software switch designed for correctness and
  debuggability, not for performance. Its instruction-level interpreter
  loop is the bottleneck.
- **Workaround**: none — this is BMv2's design. For production workloads,
  target real P4 ASICs (Tofino, dpdk-bmv2 variants).
- **Status**: inherent; not on the p4net roadmap.
