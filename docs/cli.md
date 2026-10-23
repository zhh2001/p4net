---
description: Start a topology and use the p4net shell to inspect hosts, test connectivity, program tables, and exchange controller packets.
---

# CLI reference

The `p4net` command starts a topology and opens an interactive shell. From the shell you can run host commands, test connectivity, program switch tables, read counters, and manage multicast groups. The command is included when you [install p4net](getting-started/installation.md).

## Starting the shell

The topology file must define a module-level `topology` variable containing a `Topology` instance. It may also define `setup(net)`, which runs after the network starts and before the shell opens. The [IPv4 forwarding example](examples/l3-forwarding.md) shows both definitions.

Run either entry point with the Python environment where p4net is installed:

```bash
sudo env "PATH=$PATH" p4net topology.py
sudo env "PATH=$PATH" python -m p4net topology.py
```

Both accept the same arguments:

```text
usage: p4net [-h] [--no-shell] [--unsafe] [--log-dir LOG_DIR]
             [--pcap-dir PCAP_DIR] [--extra-compile-arg ARG] [-v]
             topology_file
```

| Option | Behavior |
| --- | --- |
| `-h`, `--help` | Print startup help and exit. |
| `--no-shell` | Start the network, run `setup(net)` if present, and wait for `SIGINT` or `SIGTERM` to shut down. |
| `--unsafe` | Skip `Topology.validate()` before startup. |
| `--log-dir DIR` | Store BMv2 logs in `DIR`. Defaults to a new temporary directory. |
| `--pcap-dir DIR` | Save per-port BMv2 captures in `DIR`. Captures are disabled when omitted. |
| `--extra-compile-arg ARG` | Forward an argument to `p4c`. Repeat for multiple arguments. For a value starting with `-`, use the form `--extra-compile-arg=-DDEBUG`. |
| `-v`, `--verbose` | Set logging to INFO. Use `-vv` for DEBUG. The default is WARNING. |

With `--pcap-dir` set, `pcap_enabled=False` in `Topology.add_switch()` excludes that switch from capture.

### Using `sudo` with a virtual environment { #note-on-sudo-and-path }

On systems where `sudo` replaces `PATH` with `secure_path`, it may not find the active environment's `p4net`, Python, or external tools. The commands above pass the current `PATH` through `env`, so both entry points use that environment.

The examples below use hosts `h1`, `h2` and switch `s1`. IPv6 commands use hosts configured with IPv6 addresses. Table names, actions, counters, and controller metadata come from the loaded P4 program. Enter each `p4net>` command on one line.

## Top-level commands

### `help` and `help <command>`

`help` lists available commands. Give a topic to see its syntax and description:

```text
p4net> help pingall
pingall [count] [timeout]

Ping every pair of hosts; print a result matrix.
```

Help topics for node commands use the literal placeholders, for example `help <host> ping` or `help <switch> table add`.

### `exit`, `quit`

Leave the shell and shut down the network. `Ctrl-D` on an empty input line does the same.

### `status`

Show whether the network is running, the host and switch counts, and the BMv2 log directory.

### `hosts`

List each host's first configured IPv4 and IPv6 addresses, followed by its interfaces. Addresses retain their prefix lengths. An unconfigured address is shown as `-`.

```text
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  fd00::1/64  h1-eth0
h2    10.0.0.2/24  fd00::2/64  h2-eth0
```

### `switches`

List each switch's gRPC address, BMv2 process PID, and log file path.

### `pingall [count] [timeout]`

Run IPv4 ping between hosts with a configured primary IPv4 address, in both directions. The matrix includes all hosts, with the row as source and column as destination. `count` defaults to `1` and `timeout` to `2.0` seconds. Both follow the [ping argument rules](#ping--count-timeout). Cells show `1` for success, `X` for failure, `-` for the same host, and `?` for an untested pair involving a host without IPv4.

```text
p4net> pingall 2 2.0
H \ H h1  h2
h1  -   1
h2  1   -
2/2 succeeded
```

### `pingall6 [count] [timeout]`

Run IPv6 ping between hosts with a configured primary IPv6 address. Hosts without one are excluded. The argument rules and output format match `pingall`. If no host has IPv6 configured, the result is `(no IPv6-equipped hosts in topology)`.

```text
p4net> pingall6
H \ H h1  h2
h1  -   1
h2  1   -
2/2 succeeded
```

### `topology graph [path] [layout=LR|TB|BT|RL] [format=png|svg|pdf|dot]`

Validate the topology and render its graph. Without `path`, print DOT source. With a path, write the selected format and return the absolute output path. `layout` sets Graphviz's `rankdir` and defaults to `LR`; `format` defaults to `png`.

`format=dot` writes the source directly. Other formats require the system `dot` executable.

```text
p4net> topology graph /tmp/topo.svg layout=TB format=svg
/tmp/topo.svg
```

## Host commands

### `<host> ping <target> [count] [timeout]`

Ping from the named host. A target host name resolves to that host's primary IPv4 address. Other targets are passed to `ping`; a string containing `:` selects IPv6. Use `ping6` to resolve a host name to IPv6.

The defaults are `count=1` and `timeout=2.0` seconds. `count` must be a positive integer and limits the number of echo requests. `timeout` must be finite and non-negative. It accepts fractional seconds, such as `0.25`, and is passed to `ping -W`. A timeout of `0` relies on the overall process wait limit.

p4net calculates the overall wait from the count, timeout, and default one-second send interval. This wait must not exceed 2,147,483 seconds. The result is `OK` if at least one reply arrives, including under partial packet loss. No replies or an execution failure produces `FAIL`.

```text
p4net> h1 ping h2
OK
p4net> h1 ping 10.0.0.2 2 2
OK
```

### `<host> ping6 <target> [count] [timeout]`

Run IPv6 ping with the same defaults as `ping`. A host name resolves to its primary IPv6 address; a host without one produces an error. You can also give an IPv6 address directly.

```text
p4net> h1 ping6 h2
OK
p4net> h1 ping6 fd00::2
OK
```

### `<host> xterm`

Open an `xterm` running `bash` in the host's namespace. This requires `xterm` and a graphical session reachable through `DISPLAY`. The process is tracked and terminated when the network stops.

```text
p4net> h1 xterm
xterm spawned (pid=12345)
```

The PID varies between runs.

### `<host> cmd <argv>...`

Run a command in the host's namespace, with a 30-second timeout. Quotes group arguments using `shlex`; the command is executed directly. Use `sh -c '...'` when you need shell features such as pipes or redirection.

Stdout is printed first, then stderr with `[stderr]` before each line. A nonzero exit status adds `[exit N]`.

```text
p4net> h1 cmd printf 'hello\n'
hello
```

### `<host> ifconfig`

Run `ip -br addr` in the host's namespace and display its stdout.

## Switch commands

### `<switch> log`

Show the path of the switch's BMv2 log file. A relative `--log-dir` produces a relative path here.

### `<switch> table list`

List the tables in the loaded P4Info. Each line contains the table name and its match-field types and names:

```text
p4net> s1 table list
MyIngress.ipv4_lpm  (lpm: hdr.ipv4.dstAddr)
```

### `<switch> table dump <table>`

Show entries in the named table. Values are formatted by field width: 32-bit values as IPv4 addresses, 128-bit values as IPv6 addresses, 48-bit values as MAC addresses, and other widths as decimal numbers. Match types add the prefix length, mask, or range. Action parameters use the same width-based formatting.

For a table containing one IPv4 forwarding entry:

```text
p4net> s1 table dump MyIngress.ipv4_lpm
#0
  table:    MyIngress.ipv4_lpm
  match:    {'hdr.ipv4.dstAddr': '10.0.0.1/32'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
```

An empty table returns `(table 'MyIngress.ipv4_lpm' is empty)`.

### `<switch> table add <table> match: <kv>... action: <name> [params: <kv>...] [priority: <n>]`

Insert an entry. `match:`, `action:`, `params:`, and `priority:` start separate sections. Match fields and action parameters use `key=value` pairs, separated by spaces or commas.

```text
p4net> s1 table add MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.5/32 action: MyIngress.set_egress_port params: port=2
ok
```

Match values follow the field types in the target switch's P4Info:

| Match type | Example |
| --- | --- |
| exact, optional | `key=123` |
| lpm | `key=10.0.0.0/24` |
| ternary | `key=0x1200&&&0xff00` |
| range | `key=100..200` |

Each component accepts the field's supported value formats, including decimal or hexadecimal integers, IP addresses, and MAC addresses. Write composite values without spaces, or quote the whole value when including spaces.

For exact/LPM-only tables, omit `priority:`. Tables with ternary or range keys require it when inserting entries. For a table with an exact destination MAC, a ternary source MAC, and a range of EtherTypes:

```text
p4net> s1 table add MyIngress.l2_match match: hdr.ethernet.dstAddr=00:00:00:00:00:02,hdr.ethernet.srcAddr=00:00:00:00:00:00&&&ff:ff:ff:ff:ff:00,hdr.ethernet.etherType=0x88b0..0x88bf action: MyIngress.set_egress_port params: port=2 priority: 17
ok
```

### `<switch> table del <table> match: <kv>... [priority: <n>]` { #table-del--match- }

Delete an entry using its match key. Use the same match-value formats as `table add`, including `value&&&mask` and `low..high`. For entries with a priority, include the original value as `priority: N`.

```text
p4net> s1 table del MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.5/32
ok
```

### `<switch> table clear <table>`

Delete all entries from the table and return `cleared N entries`.

### `<switch> counter [reset] <name> [<index>]`

Read an indirect counter cell, or use `reset` to clear it. With an index, reading returns `pkts=N bytes=M`. Without an index, it returns a table of cells, or `(no populated cells)` if the switch returns none. The P4 counter type determines which counts are recorded. Resetting without an index clears all returned cells.

```text
p4net> s1 counter reset MyIngress.ingress_pkts 1
ok
p4net> s1 counter MyIngress.ingress_pkts 1
pkts=0 bytes=0
```

### `<switch> mcast list`

List multicast groups in group-ID order, in the form `<id>: [<port>, ...]`. An empty result is `(no multicast groups)`.

### `<switch> mcast add <id> <port,port,...>`

Create a group with the specified replication ports.

```text
p4net> s1 mcast add 1 1,2,3
ok
```

### `<switch> mcast del <id>`

Delete the specified multicast group.

### `<switch> packet send <hex_payload> [metadata: <k>=<v>[,<k>=<v>...]]`

Queue a `PacketOut` on the P4Runtime StreamChannel. The payload is the complete packet, expressed as a hex string. Metadata names and widths come from the P4Info `packet_out` header; omitted fields are filled with zero.

The [CPU punt example](examples/cpu-punt.md) provides a pipeline with controller packet headers and a CPU port. Its PacketOut command is:

```text
p4net> s1 packet send ffffffffffff000000000001880b48656c6c6f metadata: egress_port=1
ok
```

### `<switch> packet listen [count=N] [timeout=T]`

Collect new `PacketIn` messages while the command runs. Return when `count` packets arrive or the total `timeout` elapses. Defaults are `count=1` and `timeout=10.0` seconds. `count` must be a positive integer. `timeout` must be finite, non-negative, and within the system wait limit; `0` means no waiting. At timeout, any packets already collected are displayed.

```text
p4net> s1 packet listen
p4net> s1 packet listen count=2 timeout=3
```

Each output line contains metadata as `[name=value]` fields followed by the packet's hex contents. Metadata fields are sorted by name. Hex contents longer than 64 characters are truncated with `...`. If no packet arrives, the result is `(no packets within 3.0s)`.

## Keyboard shortcuts

- `Ctrl-C`: cancel the input line being edited.
- `Ctrl-D` on an empty line: leave the shell and shut down the network.
- `↑` / `↓`: browse history, stored in `~/.p4net_history`.
- `Tab`: complete top-level commands, host and switch names, and node verbs such as `ping` or `table`.
