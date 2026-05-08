# p4net

A P4Runtime-native SDN simulation framework for BMv2.

Status: Pre-alpha — under active development. APIs are unstable.

## Features

- P4Runtime-native control plane.
- BMv2 `simple_switch_grpc` data plane.
- Linux network-namespace based hosts.
- veth-based links with `tc`/`netem` impairment.
- Programmable Python topology DSL.
- Interactive CLI.
- Per-port packet capture.
- No OpenFlow, no Open vSwitch, no Docker.

## Requirements

- Linux kernel >= 5.4.
- Python 3.10+.
- BMv2 and p4c installed system-wide.
- Root or `CAP_NET_ADMIN` to manage namespaces and veth devices.

## Installation

Not yet published. Clone and `pip install -e '.[dev]'`.

## Quick Start

Coming soon.

## License

Apache-2.0.
