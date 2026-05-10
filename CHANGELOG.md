# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
