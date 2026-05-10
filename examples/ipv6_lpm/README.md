# ipv6_lpm

Two hosts attached to a single switch whose pipeline forwards on a 128-bit
IPv6 LPM key. Forwarding entries are programmed at runtime from
``setup(net)`` over P4Runtime; static ND is pre-seeded so ICMP unicast
does not have to resolve neighbours.

## What it shows

- A v1model pipeline matching `lpm` on `hdr.ipv6.dstAddr` with a 128-bit
  key.
- Runtime control-plane programming of an IPv6 LPM table via
  `s1.client.insert_table_entry(...)`.
- The codec's IPv6 round-trip: `<switch> table dump` renders the entries
  as `fd00::1/128` and `fd00::2/128` instead of raw bytes.

## Files

- `ipv6_lpm.p4` — pipeline.
- `topology.py` — two hosts with IPv6 only, one switch, table entries
  installed in `setup(net)`.

## Prerequisites

- Linux, Python ≥ 3.10.
- Root privileges (network namespaces and veth creation).
- `p4c` and `simple_switch_grpc` on `PATH`.
- `pip install -e '.[dev]'` in a venv.

## Running

```
sudo python examples/ipv6_lpm/topology.py
sudo p4net examples/ipv6_lpm/topology.py
```

If `sudo` strips your venv from `PATH`, invoke through `env`:

```
sudo env "PATH=$PATH" p4net examples/ipv6_lpm/topology.py
```

## Things to try in the shell

```
pingall6                         # IPv6 connectivity matrix
h1 ping6 h2                      # IPv6 ping by host name
s1 table dump MyIngress.ipv6_lpm # human-readable IPv6 entries
s1 counter MyIngress.ipv6_pkts   # per-port packet counts
```

The `table dump` output should contain `fd00::1/128` and `fd00::2/128`
literally, decoded by `P4InfoIndex.decode_match` from raw P4Runtime
canonical bytes.
