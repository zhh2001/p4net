---
description: A pipeline that punts every dataplane packet to the controller via the CPU port. Demonstrates `<switch> packet send` and `<switch> packet listen`.
---

# CPU punt

One host, one switch, every dataplane packet punted to the controller via the CPU port (510). The controller can also inject packets via `PacketOut` with explicit egress port metadata.

## Run it

From the repository root, start the topology in the first terminal.

```sh
sudo p4net examples/cpu_punt/topology.py
```

### Receive a PacketIn

Start listening in the `p4net>` shell.

```text
s1 packet listen count=10 timeout=15
```

When `Waiting for 10 packets on s1` appears, run this in a second terminal.

```sh
sudo ip netns exec h1 ping -c 1 -W 1 10.0.0.99
```

To resolve `10.0.0.99`, `h1` sends an ARP request that the switch forwards to the controller as a PacketIn. The shell prints a line like this as each request arrives and reports how many packets it received when listening ends. No host uses `10.0.0.99`, so the ping gets no reply.

```text
[_pad0=0] [ingress_port=1] ffffffffffff000000000001080600010800060400010000000000010a000001...
```

The metadata fields come from the `packet_in` controller header. The CLI truncates long frames to 64 hex characters. Use `client.expect_packet_in()` from Python to read the full frame.

### Send a PacketOut

Wait until the listener finishes and the `p4net>` prompt returns.

To verify delivery at `h1`, start `tcpdump` in the second terminal if it is installed. The filter matches the EtherType of the example frame.

```sh
sudo ip netns exec h1 tcpdump -i h1-eth0 -n -e -X -Q in -c 1 'ether proto 0x88b5'
```

Run this as one line in the `p4net>` shell. If you started a capture, wait until it shows `listening on h1-eth0` before sending the frame.

```text
s1 packet send 00000000000102000000009988b548656c6c6f metadata: egress_port=1
```

The `egress_port=1` metadata selects the switch port connected to `h1`. The shell prints `ok`, and the capture shows a frame with EtherType `0x88b5` and the `Hello` payload.

`encode_packet_out_metadata` defaults missing P4Info metadata fields to zero, including the `_pad0` field in this example.

When finished, enter `exit` at the `p4net>` prompt to stop the topology.

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

Two `@controller_header` declarations define the metadata layout for PacketIn (controller-bound) and PacketOut (controller-injected). The parser discriminates on `std.ingress_port == CPU_PORT` to extract the `packet_out` header before ethernet. The ingress control:

- For controller-injected packets: copies `egress_port` into `std.egress_spec`, invalidates the controller header.
- For dataplane packets: sets `std.egress_spec = CPU_PORT`, validates the `packet_in` header, stamps `ingress_port`.

## Variations to try

- Register a Python handler with `s1.client.on_packet_in(handler)` and read the source MAC address and ingress port from each PacketIn.
- Use `s1.client.send_packet_out(payload, {"egress_port": 1})` from a setup script to inject a sequence of probe frames.
