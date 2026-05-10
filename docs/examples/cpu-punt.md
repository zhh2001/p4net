---
description: A pipeline that punts every dataplane packet to the controller via the CPU port. Demonstrates `<switch> packet send` and `<switch> packet listen`.
---

# CPU punt

One host, one switch, every dataplane packet punted to the controller
via the CPU port (510). The controller can also inject packets via
`PacketOut` with explicit egress port metadata.

## What you'll see

`s1 packet listen` displays incoming punts in real time as the host
emits ARP / IPv6 ND / ICMP traffic. `s1 packet send` injects a
controller-built frame back into the dataplane.

## Topology

`examples/cpu_punt/topology.py`:

```python
--8<-- "examples/cpu_punt/topology.py"
```

`cpu_port=510` on the switch is what wires the CPU port to BMv2.

## P4 program

`examples/cpu_punt/cpu_punt.p4`:

```p4
--8<-- "examples/cpu_punt/cpu_punt.p4"
```

Two `@controller_header` declarations define the metadata layout for
PacketIn (controller-bound) and PacketOut (controller-injected). The
parser discriminates on `std.ingress_port == CPU_PORT` to extract
the `packet_out` header before ethernet. The ingress control:

- For controller-injected packets: copies `egress_port` into
  `std.egress_spec`, invalidates the controller header.
- For dataplane packets: sets `std.egress_spec = CPU_PORT`,
  validates the `packet_in` header, stamps `ingress_port`.

## Run it

```
sudo p4net examples/cpu_punt/topology.py
```

In the shell:

```
p4net> s1 packet listen count=3 timeout=5
[ingress_port=1] 333300000016000000000001...
[ingress_port=1] 333300000016000000000001...
[ingress_port=1] ff02000000000000000000010002...
```

The `[ingress_port=1]` prefix is the decoded `packet_in` controller
header. The hex payload is truncated at 64 chars in the CLI; full
payload is available via `client.expect_packet_in()` from Python.

To inject a frame from the controller toward `h1`:

```
p4net> s1 packet send ffffffffffff000000000001880b48656c6c6f \
         metadata: egress_port=1
ok
```

## What's interesting

- **The BPF filter trick.** When the integration tests want to
  verify a controller-injected frame arrives at `h1`, they spawn
  `tcpdump -i h1-eth0 -c 1 ether proto 0x88B5` rather than `tcpdump
  -c 1`. Without the filter, IPv6 ND noise consumes the count-1
  slot before the test frame arrives. The example uses ethertype
  `0x88B` (a local-experimental EtherType) for exactly this reason.
- **Auto-zero-pad missing metadata.** `encode_packet_out_metadata`
  iterates every metadata field declared in the P4Info and falls
  back to `metadata.get(name, 0)` for missing keys, so `_pad0`
  doesn't need to be specified by the caller.

## Variations to try

- Register a Python handler with `s1.client.on_packet_in(handler)`
  and parse the punted Ethernet frame to drive a learning switch.
- Use `s1.client.send_packet_out(payload, {"egress_port": 1})` from
  a setup script to inject a sequence of probe frames.
- Add an `ipv4_lpm` table whose `default_action = punt()` to mix
  programmed forwarding with controller fallback.
