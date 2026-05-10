# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `Python 3.13` listed in package classifiers.
- P4Runtime CPU-port packet I/O: `P4RuntimeClient.send_packet_out`,
  `on_packet_in`, `expect_packet_in`. (#feature/v0.2-packet-io)
- CLI commands `<switch> packet send` and `<switch> packet listen`.
- `examples/cpu_punt/` demonstrating CPU-punt and controller packet injection.
- `P4InfoIndex.packet_in_metadata_schema`,
  `packet_out_metadata_schema`, `encode_packet_out_metadata`,
  `decode_packet_in_metadata`.
- IPv6 address support on host interfaces with `Host.ip6` and `Link` `ip6_a`/`ip6_b`.
- IPv6 default routes via `Host.default_route6`.
- Per-interface IPv6 sysctl gating: disabled by default unless `ip6` is set.
- Asymmetric link impairment via per-direction `bandwidth_a_to_b`/`b_to_a`,
  `delay_a_to_b`/`b_to_a`, `jitter_a_to_b`/`b_to_a`, `loss_pct_a_to_b`/`b_to_a`.
- `RunningHost.ping` auto-selects IPv4 vs IPv6 based on target.
- CLI `<host> ping6 <target>` for explicit IPv6 ping.
- CLI `hosts` output now shows IPv6 addresses when present.
- `dual_stack` and `asymmetric_link` examples.
- IPv6 codec helper `decode_ipv6` plus 128-bit-field support in
  `P4InfoIndex.decode_match` so `<switch> table dump` renders IPv6 LPM
  matches as `fd00::1/128` instead of raw bytes.
- `Network.xterm(host)` and CLI `<host> xterm` for spawning an interactive
  terminal in a host's namespace (requires `$DISPLAY`).
- `Network.pingall6()` and CLI `pingall6` for an IPv6 connectivity matrix
  over hosts with `primary_ip6` set.
- `Topology.to_graphviz()` and `Topology.render_graphviz(path, format=...)`
  with CLI command `topology graph [path] [layout=LR|TB|BT|RL] [format=png|svg|pdf|dot]`.
- `ipv6_lpm` example demonstrating IPv6 LPM forwarding via runtime table
  programming.

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
