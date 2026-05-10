# Architecture

p4net is a P4Runtime-native SDN simulation framework for BMv2. This page
explains how the codebase is organised, why the layers exist, and the
trade-offs that shaped the public API.

## Goals and non-goals

### In scope

- **P4Runtime-native control plane.** All switch programming goes through
  the standard P4Runtime gRPC service: pipeline configuration, table
  inserts/modifies/deletes, indirect counter reads, multicast group
  management.
- **BMv2 `simple_switch_grpc` data plane.** Each P4 switch is a real
  process with a P4Runtime gRPC port and an optional Thrift port for
  legacy debugging.
- **Linux netns-based hosts.** Each host is a Linux network namespace
  with its own loopback, IPv4 addresses, and veth interfaces.
- **Programmable Python topology DSL.** Topologies are described in
  Python (`Topology.add_host`, `add_switch`, `add_link`); the same code
  validates and realises the topology.
- **Interactive CLI.** `p4net <topology.py>` brings the network up and
  drops the user into a `prompt_toolkit` shell with host commands,
  table programming, counter reads, and multicast control.

### Out of scope

- OpenFlow, Open vSwitch, Mininet compatibility shims.
- Docker or any container runtime.
- Distributed/multi-host topologies — everything runs on one Linux box.
- Hardware targets (Tofino, etc.). Only the BMv2 v1model architecture is
  exercised.
- gNMI, gNOI, OpenConfig — only P4Runtime is implemented.

## Layered architecture

The package is organised into six layers, lowest to highest:

### `p4net.runtime`

System primitives. Direct wrappers around `iproute2` and BMv2:

- `NetworkNamespace` — `ip netns` lifecycle plus `subprocess.run(["ip",
  "netns", "exec", ns, ...])` for in-namespace execution.
- `VethPair` — `ip link add ... type veth` plus address/MTU/MAC ops.
- `apply_netem` — `tc qdisc add ... netem` for loss/delay/jitter
  impairments.
- `BMv2Switch` — `simple_switch_grpc` `Popen` lifecycle, including
  health probes against the gRPC port and signal-based teardown.

These primitives carry no orchestration. They fail fast with explicit
exceptions; the caller is responsible for cleanup.

### `p4net.topo`

Descriptive DSL. `Host`, `P4Switch`, `Link`, `Topology` are dataclasses
that describe the desired graph. `Topology.validate()` enforces:

- Unique node and interface names.
- Interface names within Linux's 15-character limit.
- Every link endpoint resolves to a real node.
- Each switch has a P4 source path that exists.

Validation runs at `Network.start()` time unless suppressed by the
`unsafe=True` flag.

### `p4net.compiler`

Wraps `p4c -b bmv2 --p4runtime-files=p4info.txtpb`. Output is cached
under `~/.cache/p4net/compiler/` keyed by the SHA-256 of the source plus
the compiler invocation arguments. Re-running the same source with the
same arguments is a no-op cache hit; changing either invalidates the
cache for that hash.

### `p4net.control`

P4Runtime gRPC client and codec helpers:

- `P4RuntimeClient` — master-arbitration handshake, pipeline config push,
  table CRUD, counter reads, multicast group management. Election IDs
  use millisecond-since-epoch so re-running the same script reclaims
  primary cleanly.
- `P4InfoIndex` — name → ID lookups, match-field bitwidth/match-type
  resolution, `encode_match` / `decode_match`, `encode_action`.
- `codec` — `encode_value`, `parse_lpm`, `parse_ternary`, `parse_range`,
  `decode_ipv4`, `decode_mac`, `format_lpm` / `format_ternary` /
  `format_range` / `format_exact`. The encode side accepts string,
  int, and bytes literals; the decode side preserves P4Runtime canonical
  bytes (zero-extended on the high side when necessary).

### `p4net.network`

Orchestrator. `Network(topo)` composes all the layers above:

- Validates the topology (unless `unsafe=True`).
- Creates one namespace per host.
- Creates veth pairs and moves the host-side endpoint into the namespace.
- Configures IPs and MACs, applies `netem`, brings interfaces up.
- Compiles each switch's P4 source and starts a `simple_switch_grpc`
  process in the root namespace.
- Connects a `P4RuntimeClient` per switch and pushes the pipeline.
- On exit (context-manager exit, `atexit`, or `SIGINT`/`SIGTERM`),
  reverses every step.

`Network` is the only API surface that exposes runtime objects:
`net.host(name)` returns a `RunningHost`, `net.switch(name)` returns a
`RunningSwitch` whose `.client` is the `P4RuntimeClient`.

### `p4net.cli`

Interactive shell and console script:

- `CommandDispatcher` — pure parser/executor. Takes a `Network`, accepts
  one input line, returns formatted text.
- `P4NetShell` — `prompt_toolkit` REPL: `FileHistory`,
  `NestedCompleter`, Ctrl-C cancels the current line, Ctrl-D exits.
- `main` — `argparse`-driven console script. Loads a topology file by
  path via `importlib.util.spec_from_file_location`, brings the network
  up, calls `setup(net)` if present, then either runs the shell or
  blocks on `signal.pause()` (`--no-shell` mode).

The CLI does not introduce new state; it is a thin layer over the
`Network` object.

## Architectural decisions

### BMv2 runs in the root namespace

Hosts get private namespaces; switches do not. The motivations:

- Simpler gRPC reachability — the controller can connect to the switch
  on `127.0.0.1:<port>` from the root namespace without crossing a veth.
- The Linux kernel already isolates the switch's veth peers via
  namespace placement; the switch process itself does not benefit from
  isolation.
- Mininet uses the same pattern for the same reasons.

### Topology is descriptive, Network is operational

`Topology` is a dataclass tree. `Network` is the realisation. Splitting
the two means topology validation, serialization, and equality checks
are cheap (no system calls) and the same `Topology` can be reused across
multiple `Network` instances in tests.

### Compilation is content-addressed

`P4Compiler` keys the compile cache on the SHA-256 of the P4 source plus
the literal `p4c` argument list. This means changing an `--include`
path or `--target` invalidates the cache; touching the source without
changing its bytes does not.

### `subprocess.Popen(["ip", "netns", "exec", ...])` instead of `pyroute2.NSPopen`

`pyroute2.NSPopen` performs `setns()` after `fork()`. In a Python process
that has already started P4Runtime client streaming threads, this can
deadlock the child between fork and exec. We use the kernel's own
`ip netns exec` wrapper, which `clone()`s and `execve()`s without
running Python code in the intermediate state. Phase 7 chronicled the
reproduction; the fix moved every in-namespace execution path off
`NSPopen`.

### Election IDs are millisecond-since-epoch

`P4RuntimeClient` uses `int(time.time() * 1000)` for its master election
ID. This guarantees that re-running the same script in quick succession
claims primary cleanly — the new client always has a higher ID than the
last one.

### Test markers gate root- and binary-dependent tests

Pytest markers `integration`, `requires_p4c`, `requires_bmv2` plus the
matching `--run-integration`, `--run-p4c`, `--run-bmv2` opt-in flags
keep the default test run hermetic (no root, no binaries). CI and
developers explicitly opt in to the heavier suites.

## Module map

```
src/p4net/
  runtime/         system primitives (netns, veth, netem, bmv2)
  topo/            descriptive DSL (Host, P4Switch, Link, Topology)
  compiler/        p4c wrapper with content-addressed cache
  control/         P4Runtime client + codec + P4Info index
  network/         orchestrator (Network, RunningHost, RunningSwitch)
  cli/             dispatcher, shell, completers, console script
  __init__.py      re-exports Network and __version__
  __main__.py      `python -m p4net <args>` entry point

tests/
  runtime/, topo/, compiler/, control/, network/, cli/
  fixtures/        P4 sources used by integration tests
  conftest.py      session-scoped namespace cleanup, marker plumbing
```

## What we don't have yet

These are known limitations as of v0.1.0:

- **No CPU-port packet I/O.** The CPU port is wired in `simple_switch_grpc`,
  but neither `P4RuntimeClient` nor `RunningSwitch` exposes packet-in
  / packet-out yet.
- **Symmetric link impairment only.** `Link.loss_pct` and friends apply
  symmetrically; per-direction parameters are not supported.
- **No IPv6 on host interfaces.** `Topology.add_host(ip=...)` accepts
  IPv4 CIDR only.
- **v1model architecture only.** `simple_switch_grpc` is the only target;
  PSA, TNA, and other architectures are untested.
- **No live topology mutation.** Once `Network.start()` returns, the
  graph is fixed. Adding or removing hosts/switches/links requires a
  full restart.
- **Shell `run()` loop is unexercised by tests.** Coverage on
  `cli/shell.py` sits at 43% because driving `prompt_toolkit`'s
  `PromptSession` from a unit test requires a virtual terminal we
  haven't wired up. The dispatcher (which contains the actual logic) is
  tested directly.
- **No multi-host setup.** Everything is one Linux box. Federating two
  p4net instances would require external glue.
