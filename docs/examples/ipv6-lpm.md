---
description: Two IPv6-only hosts with a 128-bit LPM table programmed at runtime. Demonstrates IPv6 codec round-tripping in `<switch> table dump`.
---

# IPv6 LPM

Two IPv6-only hosts on a single switch with an `ipv6_lpm` table that
matches a 128-bit destination address. Programmed at runtime over
P4Runtime. The interesting thing is that `<switch> table dump` renders
the IPv6 entries in human form (`fd00::1/128`) instead of as raw
canonical bytes.

## What you'll see

`pingall6` succeeds, `s1 table dump MyIngress.ipv6_lpm` shows
`fd00::1/128` and `fd00::2/128`, and the per-port counter increments
with each ping.

## Topology

`examples/ipv6_lpm/topology.py`:

```python
--8<-- "examples/ipv6_lpm/topology.py"
```

`Host.ip6` is the only L3 address — both hosts are IPv6-only. The
orchestrator runs `enable_ipv6(ns, iface)` and assigns
`fd00::1/64` / `fd00::2/64` before bringing the interfaces up.

## P4 program

`examples/ipv6_lpm/ipv6_lpm.p4`:

```p4
--8<-- "examples/ipv6_lpm/ipv6_lpm.p4"
```

Two notable bits:

- The match key is `bit<128> dstAddr` with `lpm` — the runtime layer
  stores the canonical bytes plus a prefix length, and `decode_match`
  knows to format 128-bit fields as IPv6.
- `set_egress_port` bumps an indirect counter so we can verify
  forwarded traffic from the controller.

## Run it

```
sudo p4net examples/ipv6_lpm/topology.py
```

```
p4net> hosts
name  primary_ip  primary_ip6  interfaces
h1    -           fd00::1/64   h1-eth0
h2    -           fd00::2/64   h2-eth0

p4net> s1 table dump MyIngress.ipv6_lpm
#0
  table:    MyIngress.ipv6_lpm
  match:    {'hdr.ipv6.dstAddr': 'fd00::1/128'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
#1
  table:    MyIngress.ipv6_lpm
  match:    {'hdr.ipv6.dstAddr': 'fd00::2/128'}
  action:   MyIngress.set_egress_port
  params:   {'port': '2'}

p4net> pingall6
H \ H   h1   h2
   h1    -    1
   h2    1    -
2/2 succeeded

p4net> s1 counter MyIngress.ipv6_pkts 2
pkts=1 bytes=118
```

(That table dump output is captured verbatim from the phase-13
integration test.)

## What's interesting

- **Width-aware decoding selects IPv6 format for 128-bit fields.**
  The same `decode_match` function renders 32-bit fields as IPv4,
  48-bit fields as MAC, 128-bit fields as IPv6 condensed form. No
  per-field annotations needed in the P4Info; the bitwidth is the
  hint.
- **Canonical bytes round-trip cleanly.** `encode_value("fd00::1",
  128)` produces `b'\xfd\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00
  \x00\x00\x00\x01'`. P4Runtime canonicalizes by stripping leading
  zeros, then BMv2 stores whatever bytes the controller sent. On
  read, `decode_ipv6` zero-extends on the high (most-significant)
  side back to 16 bytes and feeds `ipaddress.IPv6Address.__str__`.
  Verified by the phase-13 integration test.

## Variations to try

- Replace one `/128` entry with `/64` (covering both hosts) and
  observe how LPM resolves on the longer prefix when both are
  installed.
- Add a third host on `fd00::3/64` and install a routing entry from
  Python at runtime, without touching the P4 source or restarting.
- Use `client.read_counter("MyIngress.ipv6_pkts")` from a Python
  controller to poll counters periodically while traffic flows.
