---
description: Two hosts forwarded by an ipv4_lpm table programmed from Python at startup. Demonstrates runtime table programming.
---

# L3 forwarding

Two hosts on one switch, with the dataplane forwarding via an
`ipv4_lpm` table that's programmed from Python at startup time.

## What you'll see

`pingall` succeeds because the controller installs `/32` routes for
both hosts before the shell opens.

## Topology

`examples/l3_forwarding/topology.py`:

```python
--8<-- "examples/l3_forwarding/topology.py"
```

`setup(net)` issues two `client.insert_table_entry(...)` calls — one
per host — naming the table by its fully qualified P4Info name.

## P4 program

`examples/l3_forwarding/ipv4_lpm.p4`:

```p4
--8<-- "examples/l3_forwarding/ipv4_lpm.p4"
```

The ingress control applies `ipv4_lpm` only when an IPv4 header is
present — non-IPv4 traffic (e.g. ARP) hits the default `NoAction`
and goes nowhere. ARP works because `setup(net)` seeds it statically.

## Run it

```
sudo p4net examples/l3_forwarding/topology.py
```

Then in the shell:

```
p4net> s1 table dump MyIngress.ipv4_lpm
#0
  table:    MyIngress.ipv4_lpm
  match:    {'hdr.ipv4.dstAddr': '10.0.0.1/32'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
#1
  table:    MyIngress.ipv4_lpm
  match:    {'hdr.ipv4.dstAddr': '10.0.0.2/32'}
  action:   MyIngress.set_egress_port
  params:   {'port': '2'}

p4net> pingall
H \ H   h1   h2
   h1    -    1
   h2    1    -
2/2 succeeded

p4net> s1 counter MyIngress.ingress_pkts 1
pkts=1 bytes=98
```

The match value renders as `10.0.0.1/32` — that's `decode_match`
turning P4Runtime canonical bytes back into a human IPv4 string.

## What's interesting

- Same dataplane handles a 5-host topology, a 100-host topology, and
  a different L3 design — only the table programming changes.
- `s1.client.insert_table_entry(...)` accepts plain Python types
  (strings, dicts, ints); the `P4InfoIndex` translates them into
  P4Runtime FieldMatch and Action protos based on the loaded P4Info.

## Variations to try

- Add a third host on `10.0.0.3/24` and install a third LPM entry.
  No P4 changes needed.
- Replace the `/32` entries with a `/24` covering the whole subnet
  via the same egress port. Verify `pingall` still works.
- Add a host on `10.1.0.0/24` and drop its traffic with the
  `MyIngress.drop` action — watch the LPM resolve from longest to
  shortest prefix.
