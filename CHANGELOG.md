# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
