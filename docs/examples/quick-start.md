---
description: Connect two hosts through a P4 switch that forwards between ports 1 and 2.
---

# Quick start (port swap)

Two hosts connect to ports 1 and 2 of a BMv2 switch. The P4 program forwards frames between the two ports.

## Run it

From the repository root, start the topology.

```sh
sudo p4net examples/quick_start/quick_start.py
```

At the `p4net>` prompt, use `hosts` to list the interfaces and `pingall` to check connectivity in both directions.

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  -           h1-eth0
h2    10.0.0.2/24  -           h2-eth0

p4net> pingall
H \ H  h1   h2
h1     -    1
h2     1    -
2/2 succeeded
```

When finished, enter `exit` at the `p4net>` prompt to stop the topology.

## Topology

`examples/quick_start/quick_start.py`:

```python
--8<-- "examples/quick_start/quick_start.py"
```

`setup(net)` runs after the network starts and before the shell opens. It sets static ARP entries so the first ping can use the peer's MAC address immediately.

## P4 program

`examples/quick_start/quick_start.p4`:

```p4
--8<-- "examples/quick_start/quick_start.p4"
```

The ingress control selects the output port from the input port. This also forwards ARP frames between the two ports.

## Variations to try

- Add a third host on port 3. The current program drops frames arriving on that port and forwards traffic from ports 1 and 2 only to each other.
- Replace the ingress conditional with `mark_to_drop(std)` and restart the topology. `pingall` reports `0/2 succeeded`.
- Add `loss_pct=20.0` to the `add_link()` call for `h1` and `s1`. Compare the loss summaries from `h1 ping h2 10 1` before and after the change.
