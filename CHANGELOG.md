# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
