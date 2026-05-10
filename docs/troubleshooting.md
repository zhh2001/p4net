---
description: Common p4net failure modes — missing tools, sysctl errors, BMv2 startup hangs, gRPC issues, leftover veth pairs, IPv6 noise — with diagnoses and fixes.
---

# Troubleshooting

Each entry below documents a real failure mode that has surfaced
during p4net development or use. Symptom first, then cause, then fix.

## "Permission denied" creating namespaces / veth

!!! warning "Symptom"
    `PermissionError: [Errno 13] Permission denied` from `ip netns add`,
    `ip link add ... type veth`, or `tc qdisc add`.

**Cause.** p4net needs `CAP_NET_ADMIN` to manipulate namespaces, links,
and traffic-control state. Without root or that capability, every
network-touching call fails.

!!! tip "Fix"
    Run with `sudo`. If `sudo` strips your venv from `PATH`,
    use `sudo env "PATH=$PATH" p4net <topology.py>` or pass the
    absolute path: `sudo "$(. .venv/bin/activate && which p4net)" ...`.

    `setcap cap_net_admin+ep` on the Python interpreter works in
    principle but has nasty interactions with shebang lookup and
    setuid; not recommended unless you understand the trade-offs.

## "p4c not found" / "simple_switch_grpc not found"

!!! warning "Symptom"
    `FileNotFoundError: [Errno 2] No such file or directory: 'p4c'`
    or `'simple_switch_grpc'`.

**Cause.** The external compiler or BMv2 binary isn't on `PATH`.

!!! tip "Fix"
    Confirm with `which p4c` and `which simple_switch_grpc`. If
    they're installed but not on `PATH`, fix `PATH` in your shell or
    invoke p4net with an explicit `PATH` (`sudo env "PATH=$PATH" ...`).
    If they're not installed, see
    [Installation → Required external tools](getting-started/installation.md#required-external-tools).

## BMv2 startup hangs

!!! warning "Symptom"
    `Network.start()` hangs at "waiting for BMv2 gRPC to become ready"
    and eventually times out (default 10 s).

**Cause.** BMv2 takes longer than 10 s to bind its gRPC port. Common
reasons: the host is heavily loaded, the kernel is paging, or
`simple_switch_grpc` was started in a tight loop and the previous
instance hasn't released the port yet.

!!! tip "Fix"
    Pass a longer ready timeout to `Network`:

    ```python
    Network(topo, bmv2_grpc_ready_timeout=30.0)
    ```

    If startup still hangs, check the BMv2 log under
    `<log_dir>/<switch>.log` — the orchestrator records it. A common
    line to look for is `Could not bind: Address already in use`,
    which means a previous instance is still around.

## gRPC `UNAVAILABLE` from the P4Runtime client

!!! warning "Symptom"
    `grpc.RpcError: <_InactiveRpcError ... StatusCode.UNAVAILABLE ...>`
    on `client.connect()` or the first `set_pipeline_config` call.

**Cause.** Same family as the BMv2-startup-hangs case: the client is
trying to talk to a port BMv2 hasn't bound yet.

!!! tip "Fix"
    The orchestrator polls the gRPC port before calling `connect()`,
    so this rarely surfaces in the orchestrator-managed flow. If
    you're driving `P4RuntimeClient` directly, add your own readiness
    poll, or accept the failure and retry.

## gRPC `UNKNOWN` with cryptic byte payload

!!! warning "Symptom"
    A write fails with `grpc.RpcError(StatusCode.UNKNOWN, ...)` and
    the message contains binary data.

**Cause.** BMv2 packs per-update error details into a
`grpc-status-details-bin` trailer (a serialized `google.rpc.Status`
containing one or more `p4.v1.Error` messages with numeric canonical
codes). p4net's `P4RuntimeClient._translate_rpc_error` decodes those
into specific exceptions (`DuplicateEntryError`, `EntryNotFoundError`,
`PipelineError`, etc.), but only when the trailer is present.

!!! tip "Fix"
    Make sure you're using p4net ≥ 0.1.0 (the codec landed in phase 5).
    If you're seeing the raw `UNKNOWN` from a `client.last_error`
    access, log the full exception's `details()` and `trailing_metadata()`
    — the original status is in `grpc-status-details-bin`.

## `pyroute2.NetlinkError(17, 'File exists')` on link create

!!! warning "Symptom"
    `Network.start()` fails with `LinkError: interface 'h1-eth0'
    already exists` or similar netlink errors with errno 17.

**Cause.** A previous run left the veth pairs around. Normally the
atexit cleanup or the SIGINT handler tears them down, but neither
runs if the process was killed with `SIGKILL` (e.g. `kill -9` or OOM)
or terminated before the handlers were installed.

!!! tip "Fix"
    Verify with `ip link show` — leftover veths usually carry
    `<host>-eth<N>` names. Delete with:

    ```
    sudo ip link del h1-eth0
    sudo ip netns del h1
    ```

    Then re-run.

## IPv6 link-local addresses on switch-side veth (older versions)

!!! warning "Symptom"
    On p4net < 0.2.0, `<host> ifconfig` and `<switch> log` show stray
    `fe80::` addresses; punt-path captures show MLD chatter.

**Cause.** The Linux kernel auto-generates `fe80::` link-local
addresses on every interface that comes up while
`net.ipv6.conf.<iface>.disable_ipv6=0`. Pre-0.2.0 didn't gate the
sysctl before bringing the interface up.

!!! tip "Fix"
    Upgrade to p4net ≥ 0.2.0. The orchestrator now writes
    `disable_ipv6=1` on every interface that doesn't have an
    explicit IPv6 address before the link goes up; interfaces with
    `Host.ip6` get `disable_ipv6=0, accept_ra=0, autoconf=0`.

## Pingall fails with all `X` cells

!!! warning "Symptom"
    `pingall` returns a matrix of `X` (failure) cells; nothing reaches
    anything.

**Cause.** Most often, ARP isn't seeded. BMv2 has no ARP responder;
without static neighbors, the first ICMP request triggers an ARP
broadcast that the dataplane has no rule for.

!!! tip "Fix"
    The bundled examples seed ARP in `setup(net)`:

    ```python
    h1.exec(["ip", "neigh", "replace", "10.0.0.2",
             "lladdr", "00:00:00:00:00:02",
             "dev", "h1-eth0", "nud", "permanent"])
    ```

    Either do this for every host pair, or implement an ARP
    responder in the P4 program (e.g. via the CPU-port punt path).

## Compiler cache stale after editing P4 source

!!! warning "Symptom"
    Edits to a `.p4` file aren't reflected in the running pipeline.

**Cause.** Almost never the cache (which keys on source bytes), but
worth checking if it ever surfaces.

!!! tip "Fix"
    `rm -rf ~/.cache/p4net/compiler/` to nuke it. The next run will
    repopulate. If the problem persists, it's not the cache —
    confirm the right `.p4` path is referenced in `add_switch(p4_src=...)`.

## xterm fails with "DISPLAY not set"

!!! warning "Symptom"
    `<host> xterm` returns `error: NetworkError: cannot spawn xterm:
    $DISPLAY is unset`.

**Cause.** No X server is reachable from the orchestrator process.
`xterm` needs `$DISPLAY` to be set and an X socket reachable.

!!! tip "Fix"
    On a desktop Linux session, this works out of the box. Over SSH,
    use `ssh -X user@host` and pass `-Y` if your X server requires
    trusted forwarding. On WSL2, install an X server (VcXsrv,
    XQuartz over forwarded display) and set `DISPLAY` accordingly.
    On a headless server, `xterm` simply doesn't apply — use
    `<host> cmd <argv>` for one-shot commands.

## "Address already in use" on BMv2 gRPC port

!!! warning "Symptom"
    BMv2 fails to start with `Could not bind: Address already in use`
    in `<log_dir>/<switch>.log`.

**Cause.** Another `simple_switch_grpc` (or any other process) is
already bound to the port the orchestrator picked.

!!! tip "Fix"
    Identify and kill the holder:

    ```
    sudo ss -ltnp | grep <port>
    sudo pkill simple_switch_grpc
    ```

    The orchestrator picks ports starting at 50051; if you have a
    persistent collision, override per switch:
    `topology.add_switch("s1", ..., grpc_port=50061)`.

## `topology graph` reports "graphviz `dot` binary not found"

!!! warning "Symptom"
    `topology graph /tmp/topo.png` returns `error: TopologyError:
    graphviz \`dot\` binary not found on PATH`.

**Cause.** `graphviz` isn't installed.

!!! tip "Fix"
    Install it (`sudo apt install graphviz`), or use `format=dot` to
    emit the source verbatim:

    ```
    p4net> topology graph /tmp/topo.dot format=dot
    ```

    Then render externally with any DOT-aware tool (online viewer,
    `gxl2dot`, etc.).

## Nothing here covers your problem

Open an issue at <https://github.com/zhh2001/p4net/issues> with the
output of `python -c "import p4net; print(p4net.__version__)"`,
`p4c --version`, `simple_switch_grpc --version`, the topology
file (or a minimal reproducer), and the relevant log lines from
`<log_dir>/<switch>.log`.
