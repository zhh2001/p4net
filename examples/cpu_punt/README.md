# cpu_punt

A single host attached to a BMv2 switch whose pipeline punts every
dataplane packet to the controller via the CPU port (port 510), and
forwards controller-injected packets according to the `egress_port`
metadata field.

## What it shows

- A minimal P4 pipeline using `@controller_header("packet_in")` and
  `@controller_header("packet_out")` (`cpu_punt.p4`).
- The CLI surface for controller packet I/O: `<switch> packet listen`
  and `<switch> packet send`.
- The Python API behind the CLI: `RunningSwitch.client.send_packet_out`
  and `client.on_packet_in` / `client.expect_packet_in`.

## Files

- `cpu_punt.p4` — pipeline.
- `topology.py` — one host (`h1` on `10.0.0.1/24`) plus one switch
  (`s1` with `cpu_port=510`).

## Prerequisites

- Linux, Python ≥ 3.10.
- Root privileges (network namespaces and veth creation).
- `p4c` and `simple_switch_grpc` on `PATH`.
- `pip install -e '.[dev]'` in a venv.

## Running

```
sudo python examples/cpu_punt/topology.py
sudo p4net examples/cpu_punt/topology.py
```

If `sudo` strips your venv from `PATH`, invoke through `env`:

```
sudo env "PATH=$PATH" p4net examples/cpu_punt/topology.py
```

## Things to try in the shell

Observe punted packets while the host generates traffic:

```
p4net> h1 cmd ping -c 3 -W 1 10.0.0.99 &
p4net> s1 packet listen count=3 timeout=5
[ingress_port=1] ffffffffffff000000000001...
[ingress_port=1] ffffffffffff000000000001...
[ingress_port=1] ffffffffffff000000000001...
```

Inject a packet from the controller out port 1 (toward `h1`):

```
p4net> s1 packet send ffffffffffff000000000001880b48656c6c6f \
         metadata: egress_port=1
ok
```

The hex blob is a complete Ethernet frame; the `egress_port` metadata
selects the egress port via the pipeline's `packet_out` header.

## Where to read the punt logic

Open `cpu_punt.p4`. The `MyIngress` control discriminates on
`std.ingress_port`: from the CPU port, it copies `packet_out.egress_port`
into `std.egress_spec` and invalidates the controller header so it does
not appear on the wire; from any other port, it sets
`std.egress_spec = CPU_PORT` and validates the `packet_in` header with
the correct `ingress_port` so the controller can attribute the punt.
