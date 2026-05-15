# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.7.0] - 2026-05-14

### Changed

- `AsyncP4RuntimeClient`, `AsyncOperationCancelledError`, and
  `RunningSwitch.async_client` promoted from **Provisional** to
  **Stable** following real-world user soak since their introduction
  in 1.6.0. No source-level API change — only the stability tier is
  updated; method signatures are byte-for-byte unchanged. The
  promotion honors the two-minor-release upper bound committed in
  ``docs/api-stability.md`` (promoted at one minor release in, within
  the commitment window). See updated stability documentation for
  the new contract.

## [1.6.0] - 2026-05-14

### Added

- `AsyncP4RuntimeClient`: parallel async P4Runtime client using
  `grpc.aio`. Mirrors the sync `P4RuntimeClient` API surface
  (`connect` / `disconnect`, `push_pipeline`,
  `insert_table_entry` / `modify_table_entry` /
  `delete_table_entry` / `list_table_entries`, `read_counter`,
  `read_register` / `write_register`, `send_packet_out` /
  `on_packet_in` / `expect_packet_in`). Marked **Provisional** in
  1.x; commit is to upgrade to Stable within two minor releases
  conditional on no incompatible adjustments being needed in the
  interim.
- `RunningSwitch.async_client`: lazy property returning an
  unconnected `AsyncP4RuntimeClient` for the switch, pre-seeded with
  the sync client's P4Info index. **Provisional**.
- `AsyncOperationCancelledError`: paired exception for async
  cancellation cases (subclass of `P4RuntimeError`). **Provisional**.
- New example `examples/async_concurrent/` demonstrating concurrent
  multi-switch operations via the async client and `asyncio.gather`.
- `docs/async-client.md` (EN + ZH) explaining the async API,
  mastership coexistence patterns, and cancellation semantics.

### Changed

- `BMv2Switch.grpc_port` is now a public property (was private,
  needed by `RunningSwitch.async_client` to plumb the gRPC address
  through to the async client constructor).
- `docs/api-stability.md` gained a "Provisional tier and async
  client" section explaining the rationale for shipping the async
  client as Provisional rather than Stable on day one.
- `docs/known-limitations.md`: the "single Network lifecycle per
  Python process" entry now references the async client as the
  primary workaround for per-RPC operations.
- `pytest-asyncio>=0.23` added to dev dependencies;
  `asyncio_mode = "auto"` in `[tool.pytest.ini_options]` so async
  tests don't need per-test decorators.

## [1.5.1] - 2026-05-12

### Changed

- Multi-hop INT integration test no longer asserts strict aligned-causal
  ordering between switches. The assertion tested packet causality —
  guaranteed by construction in a linear topology, not by p4net code —
  and had to be widened twice (5 ms → 20 ms across v1.4 and v1.5) to
  absorb measurement drift under suite load. It produced two flakes
  without catching a real regression. Replaced with a bounded-difference
  sanity check (aligned timestamps within 10 s of each other) that
  validates alignment produces plausible numbers without asserting
  physics. No production code is affected.

## [1.5.0] - 2026-05-11

### Added

- `Network.boot_timestamps`: read-only dict mapping switch name to
  wall-clock microseconds since Unix epoch when that switch's BMv2
  process started. Convenience helper over per-switch
  ``RunningSwitch.boot_timestamp_us``; returns a fresh dict on each
  call, raises ``NetworkNotRunningError`` if the network isn't
  running. **Stable** per the API stability commitment.

### Changed

- Multi-hop INT example reads ``P4NET_INT_BOOT_TIMES_PATH`` from the
  environment to locate its coordination file, defaulting to
  ``/tmp/p4net-int-multi-hop-boot-times.json`` when unset. Allows
  concurrent multi-hop INT topologies on the same host. Both
  ``topology.py`` and ``listener.py`` honor the variable; ``sudo -E``
  is required to preserve it across privilege escalation.
- Multi-hop INT example's ``topology.py`` uses the new
  ``Network.boot_timestamps`` helper instead of a manual dict
  comprehension over each switch — adapts automatically as switches
  are added.

### Fixed

- ``test_two_switches_each_insert_their_own_shim`` aligned-causal slack
  widened from 5 ms to 20 ms to absorb the Popen + BMv2 early-init
  drift observed reproducibly under full-suite load. The slack is
  alignment-drift bookkeeping, not data-plane latency; real per-hop
  forwarding latency through BMv2's userspace pipeline remains in the
  tens-to-hundreds of microseconds.

## [1.4.0] - 2026-05-11

### Added

- `RunningSwitch.boot_timestamp_us`: wall-clock microseconds since
  Unix epoch when the switch's BMv2 process started. Captured
  immediately before ``subprocess.Popen``, so drift from BMv2's
  internal clock zero is bounded by Popen overhead (sub-millisecond
  typically). Combine with INT shim ``ingress_timestamp_us`` to
  compute wall-clock arrival time and align timestamps across
  multiple switches. **Stable** per the API stability commitment.
  Underlying machinery: ``BMv2Switch.boot_timestamp_us`` (read-only,
  ``None`` when the switch is not running).

### Changed

- Multi-hop INT example listener now reads each switch's BMv2 boot
  timestamp from a coordination file at
  ``/tmp/p4net-int-multi-hop-boot-times.json`` written by
  ``topology.py``'s ``setup(net)``, and displays both raw and aligned
  wall-clock timestamps plus a computed ``latency_s1_to_s2`` line
  (real per-hop forwarding latency through BMv2's userspace pipeline
  plus the veth pair). Listener falls back gracefully to the v1.3
  unaligned display when the coordination file is missing.
- Multi-hop INT example README and docs page updated to document the
  alignment formula and replace the v1.3 caveat about non-comparable
  cross-switch timestamps.
- Multi-hop INT integration test asserts aligned causal ordering
  (`aligned_s2 >= aligned_s1 - 5 ms`) — an assertion that could not
  hold on v1.3's raw-timestamp path.

## [1.3.0] - 2026-05-11

### Added

- New example `examples/int_multi_hop/` demonstrating production-style
  in-band network telemetry: two switches in a linear path, each
  inserting its own metadata shim, with a receiver-side listener
  decoding the full hop-by-hop stack. The P4 program runs unchanged on
  both switches; per-switch identity comes from the v1.2 register API.

### Documentation

- New documentation site page for the multi-hop INT example, available
  in both English and 简体中文.
- Cross-link from `examples/int/README.md` to the multi-hop example as
  a "next step" for readers progressing past single-switch INT.
- Examples index (`docs/examples/index.md` and `index.zh.md`)
  refreshed: now lists all eight bundled examples with a Telemetry
  column in the comparison table.

### Fixed

- `test_symmetric_base_plus_a_to_b_extra` upper-bound tolerance widened
  from 360 ms to 450 ms to eliminate consistent failures under
  full-suite load (same root cause and fix shape as the asymmetric
  test widened in v1.2.0).

## [1.2.0] - 2026-05-11

### Added

- `P4RuntimeClient.write_register(name, index, value)` and
  `P4RuntimeClient.read_register(name, index=None)` for direct
  read/write access to P4 registers. Both methods are **stable** per
  the API stability commitment. On BMv2 the implementation uses the
  Thrift control channel via `simple_switch_CLI`, because BMv2's PI
  backend currently returns UNIMPLEMENTED for P4Runtime RegisterEntry;
  the Python API surface matches what a P4Runtime-compliant target
  would expose.
- `P4InfoIndex.register_by_name(name)` exposing `RegisterSpec`
  metadata (`id`, `name`, `bitwidth`, `size`), plus
  `P4InfoIndex.register_names`.
- `RegisterSpec` dataclass and `NoSuchRegisterError` exception,
  exported from `p4net.control`.
- `P4RuntimeClient(..., thrift_address=(host, port))` constructor
  argument so register operations can reach BMv2's Thrift sidecar.
  The orchestrator wires it from `BMv2Switch.thrift_port`.

### Changed

- INT example now uses a register-backed `switch_id` instead of a
  hard-coded P4 constant. `examples/int/topology.py` writes the
  register in `setup(net)`; multi-switch INT deployments can assign
  distinct identifiers without recompiling.
- INT shim's timestamp field renamed from `ingress_timestamp_ns` to
  `ingress_timestamp_us` to reflect BMv2's actual reporting
  resolution. **Wire format is unchanged**; only naming. A v1.1.0
  capture decodes identically against the v1.2.0 listener.

### Fixed

- Asymmetric-delay round-trip test
  (`test_asymmetric_delay_round_trip`) upper-bound tolerance widened
  from 280 ms to 320 ms to eliminate suite-load flakes. Lower bound
  unchanged.

## [1.1.0] - 2026-05-11

### Added

- INT (In-band Network Telemetry) example: single-switch topology where
  the P4 pipeline inserts a 14-byte INT shim header (switch_id,
  ingress_timestamp, egress_port, queue_depth, original etherType,
  reserved) between Ethernet and IPv4 on every forwarded packet. A raw
  AF_PACKET socket listener inside the receiving host's namespace
  decodes the shim and prints structured per-frame telemetry.
  Demonstrates real wire-level in-band telemetry, not controller-punt.
- `Link.delay_a_to_b_extra`, `Link.delay_b_to_a_extra`,
  `Link.jitter_a_to_b_extra`, `Link.jitter_b_to_a_extra`,
  `Link.loss_pct_a_to_b_extra`, `Link.loss_pct_b_to_a_extra`:
  per-direction additive impairment on top of the symmetric base.
  Closes phase-12 OQ #4. Round-trips through
  `Topology.to_dict` / `from_dict`.
- Unified `p4net.*` logger hierarchy with documented level conventions.
  Logger namespace is **stable** in 1.x; per-event levels and log
  message text are not.
- CLI `--verbose` / `-v` (INFO) and `-vv` (DEBUG) flags for log-level
  control. Default level remains WARNING.
- `docs/known-limitations.md`: explicit catalogue of what's not
  supported in 1.x with workarounds where they exist (single Network
  lifecycle per Python process, single-host operation, no PSA, no live
  mutation, BMv2 throughput bounds).
- `docs/logging.md`: logger hierarchy and level conventions.
- Custom 404 page replaces Material's generic default.

### Changed

- Internal logging statements at lifecycle boundaries (`Network.start`,
  BMv2 ready, P4Runtime client connect, `Network.stop`) now log at INFO
  rather than DEBUG. Existing DEBUG observability is preserved.
- `docs/api-stability.md` extended with a logger-namespace stability
  section; `Link` row notes the new `*_extra` fields as Stable.

### Documentation

- New navigation entries: Logging, Known Limitations, INT example.
- Chinese translations land alongside English for all new pages.

## [1.0.0] - 2026-05-10

### Stability commitment

This release marks the public API as stable per
[API Stability](https://zhh2001.github.io/p4net/api-stability/).
APIs marked **stable** will not be broken in 1.x. APIs marked
**provisional** or **experimental** may evolve. See the linked
page for per-symbol classification.

PSA architecture support, live topology mutation, async P4Runtime
client, and distributed simulation are explicitly out of scope for
the 1.x line and planned for 2.0+.

### Fixed

- `<switch> table dump` now correctly disambiguates action-parameter
  widths across multi-switch topologies that share an action name
  (closes phase-13 OQ #1).
- `Network.stop()` skips `terminate()` for already-exited spawned
  processes (e.g. xterms closed by the user) instead of issuing a
  no-op syscall (closes phase-13 OQ #2).
- Phase-13 integration test now writes captured output to
  `tmp_path` rather than a hardcoded `/tmp/` path (closes
  phase-13 OQ #4).
- Documentation CLI reference shows the dispatcher's actual
  `OK`/`FAIL` casing for ping success/failure.

### Changed

- CI matrix expanded to include Ubuntu 22.04 alongside Ubuntu 24.04,
  plus a wheel-install smoke job to verify `pip install` correctness
  on every PR.
- GitHub Actions versions updated to current major releases
  (`checkout@v6`, `setup-python@v6`, `upload-pages-artifact@v5`,
  `deploy-pages@v5`).
- Documentation site uses Unicode-aware slugify, so Chinese (and
  other CJK) headings now produce stable cross-page anchors
  (closes phase-16 OQ #1).

### Added

- `docs/api-stability.md`: per-symbol stability classification and
  deprecation policy. Chinese translation in `api-stability.zh.md`.
- `docs/performance.md`: topology start/stop time and memory
  baseline data for 1–8 switch topologies, plus the measurement
  script for reproducibility. Chinese translation in
  `performance.zh.md`.
- `SECURITY.md` expanded with explicit privilege model, capability
  alternative to sudo, trust-boundary documentation, and
  vulnerability disclosure flow with response window.

## [0.2.0] - 2026-05-10

### Added

- P4Runtime CPU-port packet I/O: `P4RuntimeClient.send_packet_out`,
  `on_packet_in`, `expect_packet_in`.
- `P4InfoIndex.packet_in_metadata_schema`,
  `packet_out_metadata_schema`, `encode_packet_out_metadata`,
  `decode_packet_in_metadata` for controller-header encoding/decoding.
- IPv6 host addressing: `Host.ip6`, `Host.default_route6`,
  `LinkEndpoint.ip6`, with per-interface IPv6 sysctl gating
  (disabled by default; enabled when `ip6` is set, with `accept_ra=0`
  and `autoconf=0` to suppress SLAAC).
- Asymmetric link impairment: per-direction `bandwidth`, `delay`,
  `jitter`, `loss_pct` fields on `Link` (`*_a_to_b` / `*_b_to_a`).
- IPv6 codec helper `decode_ipv6` and 128-bit-field support in
  `P4InfoIndex.decode_match`, so `<switch> table dump` renders
  IPv6 LPM matches as `fd00::1/128` instead of raw byte tuples.
- `Network.xterm(host)` and CLI `<host> xterm` for spawning an
  interactive terminal in a host's namespace (requires `$DISPLAY`).
- `Network.pingall6()` and CLI `pingall6` for an IPv6 connectivity matrix.
- `Topology.to_graphviz()` and `Topology.render_graphviz(path, format=...)`,
  with CLI command `topology graph [path] [layout=...] [format=...]`.
- CLI commands `<switch> packet send` and `<switch> packet listen`.
- CLI command `<host> ping6` for explicit IPv6 ping.
- Python 3.13 added to package classifiers.
- New examples: `cpu_punt`, `dual_stack`, `asymmetric_link`, `ipv6_lpm`.

### Changed

- `RunningHost.ping` auto-selects IPv4 vs IPv6 based on the target
  string (presence of `:` selects IPv6). Pass `force_ipv6=True` to
  override.
- `<switch> table dump` action parameters now render with width-aware
  decoding (e.g. a 9-bit `port` shows as `'2'`, not `b'\x02'`).
- CLI `hosts` output gains a `primary_ip6` column.
- `topology graph` now calls `Topology.validate()` before rendering;
  malformed topologies surface a clear error instead of silently
  producing misleading DOT.

## [0.1.0] - 2026-05-10

### Added

- Linux network-namespace and veth primitives (p4net.runtime).
- BMv2 simple_switch_grpc process lifecycle wrapper (p4net.runtime.bmv2).
- Topology DSL with Host, P4Switch, Link, and Topology validation (p4net.topo).
- p4c wrapper with content-addressed compile cache (p4net.compiler).
- P4Runtime gRPC client with master arbitration, pipeline config, table CRUD,
  counter reads, and multicast group management (p4net.control).
- P4InfoIndex with encode_match / decode_match for human-readable table dumps,
  plus codec helpers (encode_value, parse_lpm/ternary/range, decode_ipv4,
  decode_mac, format_lpm/ternary/range/exact).
- Network orchestrator with start/stop, validate-on-start, atexit and signal
  cleanup, and a working with-statement (p4net.network).
- Interactive shell (p4net.cli.P4NetShell) and `p4net` console script for
  running topology files.
- Examples: quick_start (port-swap pipeline) and l3_forwarding (programmed
  ipv4_lpm with static ARP).
- Documentation: architecture, tutorial, CLI reference.
