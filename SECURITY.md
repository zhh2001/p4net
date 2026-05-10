# Security Policy

## Reporting a vulnerability

Please report suspected security issues privately by email to
`1652709417@qq.com`. Do not file public issues for unreported
vulnerabilities.

**Expected response window:**

- Acknowledgment within 14 days of receipt.
- Fix or coordinated-disclosure timeline within 90 days, depending on
  complexity.

PGP key: not currently published. If you need an encrypted channel
before disclosing, request one in your initial email and the maintainer
will provide a key.

## Scope

p4net is research and laboratory software. It is not intended for
production network deployment, and no guarantees are made about its
suitability for such use.

That said, p4net runs as root on the host machine and orchestrates
real network namespaces, real veth devices, real `tc` queueing
disciplines, and real BMv2 processes. Operational mistakes can affect
the host's networking stack. The sections below document the
privilege model and trust boundaries so that operators can reason
about the attack surface.

## Privilege model

The following operations require `root` or `CAP_NET_ADMIN`:

- Creating and destroying network namespaces (`ip netns add` /
  `ip netns del`).
- Creating, configuring, and destroying veth pairs (`ip link add type
  veth`, `ip addr add`, `ip link set up/down`, `ip link del`).
- Programming traffic-control queueing disciplines (`tc qdisc add ...
  netem`).
- Writing per-interface `sysctl` knobs
  (`net.ipv6.conf.<iface>.disable_ipv6` etc.).
- Binding TCP and UDP ports for BMv2's gRPC and Thrift services.
- Running BMv2 itself (it uses `cap_net_admin` for raw-socket data-path
  ingress/egress).

Most users invoke p4net via `sudo`. The orchestrator does not require
`CAP_SYS_ADMIN` for its own work; it does invoke `ip netns exec`,
which the kernel implements with `setns()`, but that path runs in
the user's privilege envelope rather than escalating.

## Capability alternative to `sudo`

A common alternative is to grant `CAP_NET_ADMIN` to the Python
interpreter directly:

```
sudo setcap 'cap_net_admin,cap_sys_admin+ep' "$(readlink -f $(which python3))"
```

**Trade-off.** This grants the capability to *every* invocation of
that Python interpreter, including untrusted scripts. If the
interpreter is shared across the system, prefer a dedicated venv
interpreter:

```
python3 -m venv /opt/p4net-venv
sudo setcap 'cap_net_admin,cap_sys_admin+ep' /opt/p4net-venv/bin/python
```

The capability persists across `pip install` calls into that venv,
but other Python interpreters on the system remain unprivileged.

Note: file capabilities are stripped on `setuid`-style boundaries and
on shebang resolution to a different interpreter. If `p4net --help`
fails to find the binary under `sudo`, the
[Installation guide](https://zhh2001.github.io/p4net/getting-started/installation/)
documents the explicit-`PATH` workaround.

## Trust boundaries

- **`topology.py` is arbitrary Python.** It runs in your user
  context (or root, when invoked under `sudo`). Trust topology
  files the same way you trust any Python script — review before
  executing topologies received from third parties.
- **P4 source files** are processed by `p4c`, which has its own
  sandboxing characteristics. p4net does not isolate `p4c`; the P4
  ecosystem is responsible for compiler-level safety. For p4c
  vulnerabilities, see the
  [p4lang/p4c security policy](https://github.com/p4lang/p4c/security).
- **BMv2 listens on `127.0.0.1` by default.** The orchestrator binds
  gRPC (and optionally Thrift) ports to loopback. Do not change
  this binding to `0.0.0.0` without an external firewall in place;
  exposing BMv2 on a network interface gives any client on that
  network full pipeline-write access via P4Runtime.
- **Control-plane gRPC traffic between the Python process and BMv2
  is unencrypted loopback.** This is appropriate for a single-host
  lab. If you ever bridge this traffic across hosts (e.g. by
  forwarding the gRPC port over SSH), use TLS or a transport-level
  authenticated channel.
- **The compiler cache directory** (`~/.cache/p4net/compiler/`) is
  written under the invoking user's home. Do not share the cache
  across users — it is keyed by source content hash but does not
  attest who produced an entry.

## Supported versions

| Version | Status                                                       |
| ------- | ------------------------------------------------------------ |
| 1.x     | Supported. Security fixes shipped as patch releases.         |
| 0.2.x   | Security fixes only, until 2026-12-31.                       |
| 0.1.x   | Unsupported.                                                 |

Patch and minor releases follow the deprecation policy described in
the [API Stability page](https://zhh2001.github.io/p4net/api-stability/).
