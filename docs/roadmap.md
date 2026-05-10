# Roadmap

## About this document

This is a living roadmap, not a commitment. Items are listed in rough
priority order within each release; relative ordering can change as
issues are filed and trade-offs become clearer. PRs for any item are
welcome — please comment on (or open) the linked issue first so that
design discussion happens once, in public, before the code lands.

## v0.1.x (patch releases)

Pure bug fixes and small clarifications. No API changes.

- Drive `P4NetShell.run()` from a `prompt_toolkit.input.DummyInput` /
  `DummyOutput` pair to lift `cli/shell.py` coverage from 43% into the
  same band as the rest of the package.
- Clearer error messages on common BMv2 startup failures (port already
  bound, missing `simple_switch_grpc` on `PATH`, `--no-cli` flag
  unsupported on the local BMv2 build).
- Documentation typos and tutorial polish.

## v0.2.0 (next minor release)

### CPU-port packet I/O

Extend `P4RuntimeClient` with `send_packet_out(payload, metadata)` and
a callback registration `on_packet_in(handler)` driven by the existing
StreamChannel consumer thread. Add CLI commands `<switch> packet send
<hex_payload>` and `<switch> packet listen [count]`. Required for any
P4 program that uses CPU-port punt/inject — controller-based ARP
responders, learning switches, in-network DHCP, BGP-injection demos.
The wire format is already specified by P4Runtime; the controller side
just needs to plumb metadata through the codec.

### IPv6 on host interfaces

Accept IPv6 CIDRs in `Host.ip` and `LinkEndpoint.ip`; add
`set_address6` to `VethPair`; update the address-application path in
`Network.start()` to dispatch on family. The CIDR's address family
auto-selects the right code path so existing IPv4 topologies need no
changes. Useful for any P4 program that processes IPv6 headers
(SRv6, ND-based learning, DC underlay emulation).

### Asymmetric link impairment

`Link.bandwidth_a_to_b` / `Link.bandwidth_b_to_a` (and the same split
for `delay_ms` / `loss_pct`) so traffic in one direction can be shaped
differently from the reverse direction. Falls back to the symmetric
`Link.bandwidth` when the per-direction fields are unset, preserving
v0.1.0 behavior. Useful for emulating WAN links, ADSL/cable
asymmetries, and one-direction failures during chaos testing.

### `xterm` host shell helper

A `Network.xterm(host)` method, plus a CLI command `<host> xterm`,
that spawns an `xterm` connected to the host's namespace — same UX
Mininet users are accustomed to. Requires an X server reachable through
`$DISPLAY`; fails with a clear error message rather than hanging if
`$DISPLAY` is unset or the X socket is unreachable. Useful in
classroom and demo settings where students want a shell-per-host on a
single display.

### Topology visualizer

A `Topology.to_graphviz()` method emitting DOT, plus a CLI command
`topology graph [path.png]` that renders to PNG via the `dot` binary
(if present on `PATH`). Useful for teaching, for diffing topology
changes in PRs, and for embedding diagrams in papers. Falls back to
plain DOT text if `dot` isn't installed.

## v0.3.0 (later, exploratory)

### PSA architecture support

Beyond v1model. Requires `p4c` and BMv2 with the PSA target compiled
in (not standard in many distributions). The compiler wrapper would
need to learn how to select the architecture and the `simple_switch`
process variant; `P4InfoIndex` should keep working unchanged because
P4Runtime isolates the architecture from the controller.

### Live topology mutation

`Network.add_host(...)`, `Network.add_link(...)`, and their `remove_*`
counterparts while the network is already running. Currently the
`Topology` is frozen at `start()` time. The realisation primitives
(namespaces, veth pairs) already support live add/remove; the
orchestrator's bookkeeping just needs to re-enter the same paths.

### Async P4Runtime client

An `aio-grpc`-based variant of `P4RuntimeClient` for callers building
event-driven controllers (e.g. those subscribing to many switches'
StreamChannels concurrently). Probably co-exists with the threaded
client rather than replacing it; same `P4InfoIndex` underneath.

## Deferred indefinitely

- **Distributed simulation across multiple machines.** A v1.0
  conversation; out of scope for v0.x. Federating two p4net instances
  needs L2 tunneling between hosts and a coordination layer; nothing
  in the current architecture is designed for it.
- **Web UI for topology visualization and live state.** Likely a
  separate companion repo if it ever happens. Keeps p4net itself
  framework-free.
