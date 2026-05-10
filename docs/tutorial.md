# Tutorial

This tutorial walks through p4net from a single-host topology up to a
two-switch network with runtime-programmed forwarding, link impairment,
and counter reads. Every snippet is runnable.

## 1. Prerequisites

- Linux kernel ≥ 5.4 (`uname -r`).
- Python ≥ 3.10.
- `iproute2` — already present on every modern distribution.
- `p4c` and `simple_switch_grpc` available on `PATH`. See
  [p4lang.org](https://p4.org) for distribution-specific install
  instructions.
- Root or `CAP_NET_ADMIN` so the orchestrator can create network
  namespaces and veth devices.

Verify the prerequisites:

```
$ uname -r
6.6.87.2-microsoft-standard-WSL2
$ python3 --version
Python 3.12.3
$ p4c --version
$ simple_switch_grpc --version
```

## 2. Installation

From a fresh checkout:

```
git clone https://github.com/zhh2001/p4net
cd p4net
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

`p4c` and BMv2 are external. p4net does not bundle or install them.

## 3. Hello world: a single host

A `Topology` is a description; a `Network` realises it. The smallest
useful program is a single host in its own namespace:

```python
from p4net import Network
from p4net.topo import Topology

topo = Topology()
topo.add_host("h1", ip="10.0.0.1/24")

with Network(topo) as net:
    h1 = net.host("h1")
    print(h1.exec(["ip", "addr", "show", "lo"]).stdout)
```

Run as root:

```
sudo .venv/bin/python tutorial_step3.py
```

You should see the loopback interface inside `h1`'s namespace. The
context manager tears the namespace down on exit.

## 4. Two hosts, one switch, hardcoded forwarding

The `examples/quick_start/` tree is the canonical first network. Its
P4 program (`quick_start.p4`) is a port-2-port swap (port 1 ↔ port 2)
so no runtime table programming is needed for hosts on opposite ports
to reach each other:

```python
from pathlib import Path
from p4net import Network
from p4net.topo import Topology

HERE = Path("examples/quick_start").resolve()

topo = Topology()
h1 = topo.add_host("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
h2 = topo.add_host("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
s1 = topo.add_switch("s1", p4_src=HERE / "quick_start.p4")
topo.add_link(h1, s1, port_b=1)
topo.add_link(h2, s1, port_b=2)

with Network(topo) as net:
    print(net.pingall())
```

Run as root:

```
sudo .venv/bin/python examples/quick_start/quick_start.py
```

`pingall()` returns a `dict[(src, dst), bool]` indicating whether each
unordered pair could reach each other. With static ARP pre-seeded by
the example's `setup(net)`, both directions succeed.

## 5. Programming forwarding tables

When the data plane has a real LPM table — as in `examples/l3_forwarding/`
— the controller installs entries at runtime via P4Runtime. The
relevant slice of the example's `setup(net)`:

```python
def setup(net):
    s1 = net.switch("s1")
    s1.client.insert_table_entry(
        table="MyIngress.ipv4_lpm",
        match={"hdr.ipv4.dstAddr": "10.0.0.1/32"},
        action="MyIngress.set_egress_port",
        params={"port": 1},
    )
    s1.client.insert_table_entry(
        table="MyIngress.ipv4_lpm",
        match={"hdr.ipv4.dstAddr": "10.0.0.2/32"},
        action="MyIngress.set_egress_port",
        params={"port": 2},
    )
```

`P4RuntimeClient.insert_table_entry` accepts plain Python types
(strings, dicts, ints) and uses `P4InfoIndex` to encode them into
P4Runtime FieldMatch and Action protos. LPM, ternary, range, exact, and
optional match types are all supported.

Run the example:

```
sudo .venv/bin/python examples/l3_forwarding/topology.py
```

## 6. Reading counters

Indirect counters declared in P4 (e.g. `counter(N, CounterType.packets_and_bytes)`)
are readable from the controller. From the interactive shell:

```
p4net> pingall
2/2 succeeded
p4net> s1 counter MyIngress.ingress_pkts 0
pkts=2 bytes=196
```

`<switch> counter <name> [<index>]` reads one cell of an indirect
counter. Without an index, it returns every populated cell.

## 7. Multicast groups

Multicast in v1model means setting `standard_metadata.mcast_grp` from
the data plane and registering a replication list (group ID → list of
egress ports) from the controller. From the shell:

```
p4net> s1 mcast add 1 1,2,3
ok
p4net> s1 mcast list
1: [1, 2, 3]
```

A P4 program that sets `std_meta.mcast_grp = 1` then sends a packet
will replicate it out ports 1, 2, and 3.

## 8. Link impairment

`Link.loss_pct`, `Link.delay_ms`, and `Link.jitter_ms` apply `tc netem`
to both veth ends of a link. To inject 10% loss on the h1↔s1 link:

```python
topo.add_link(h1, s1, port_b=1, loss_pct=10.0)
```

After bring-up, `pingall()` will report a sub-100% success rate on the
affected pair. The impairment is symmetric: there is no per-direction
control as of v0.1.0.

## 9. Where to go next

- [`docs/cli.md`](cli.md) — full CLI reference with every command.
- [`docs/architecture.md`](architecture.md) — what each module is for
  and the design decisions behind it.
- `examples/` — runnable topologies you can copy and modify.
- `tests/network/test_orchestrator_integration.py` and
  `tests/cli/test_dispatcher_integration.py` — end-to-end tests that
  exercise the full stack and double as worked examples.
