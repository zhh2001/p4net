# cpu_punt

A single host attached to a BMv2 switch whose pipeline punts every dataplane packet to the controller via the CPU port (port 510), and forwards controller-injected packets according to the `egress_port` metadata field.

## What it shows

- A minimal P4 pipeline using `@controller_header("packet_in")` and `@controller_header("packet_out")` (`cpu_punt.p4`).
- The CLI surface for controller packet I/O: `<switch> packet listen` and `<switch> packet send`.
- The Python API behind the CLI: `RunningSwitch.client.send_packet_out` and `client.on_packet_in` / `client.expect_packet_in`.

## Files

- `cpu_punt.p4` — pipeline.
- `topology.py` — one host (`h1` on `10.0.0.1/24`) plus one switch (`s1` with `cpu_port=510`).

## Prerequisites

- Linux, Python ≥ 3.10.
- Root privileges (network namespaces and veth creation).
- `p4c` and `simple_switch_grpc` on `PATH`.
- [p4net CLI installed with pipx](https://zhh2001.github.io/p4net/getting-started/installation/#install-cli).

## Running

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

The ping times out because `10.0.0.99` has no host. The switch punts the ARP request from `h1`, so the listener shows a line like this as soon as the request arrives. IPv6 frames may also appear. The shell prints the received count when listening ends.

```text
[_pad0=0] [ingress_port=1] ffffffffffff000000000001080600010800060400010000000000010a000001...
```

### Send a PacketOut

To verify delivery at `h1`, start `tcpdump` in the second terminal if it is installed. The filter matches the EtherType of the example frame.

```sh
sudo ip netns exec h1 tcpdump -i h1-eth0 -n -e -X -Q in -c 1 'ether proto 0x88b5'
```

Run this as one line in the `p4net>` shell. If you started a capture, wait until it shows `listening on h1-eth0` before sending the frame.

```text
s1 packet send 00000000000102000000009988b548656c6c6f metadata: egress_port=1
```

The `egress_port=1` metadata selects the switch port connected to `h1`. The shell prints `ok`, and the capture shows a frame with EtherType `0x88b5` and the `Hello` payload.

## Where to read the punt logic

Open `cpu_punt.p4`. The `MyIngress` control discriminates on `std.ingress_port`: from the CPU port, it copies `packet_out.egress_port` into `std.egress_spec` and invalidates the controller header so it does not appear on the wire; from any other port, it sets `std.egress_spec = CPU_PORT` and validates the `packet_in` header with the correct `ingress_port` so the controller can attribute the punt.
