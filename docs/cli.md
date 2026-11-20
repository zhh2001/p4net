---
description: Start a topology and use the p4net shell to inspect hosts, test connectivity, program tables, and exchange controller packets.
---

# CLI reference

The `p4net` command starts a topology and opens an interactive shell. From the shell you can run host commands, test connectivity, program switch tables, read counters, and manage multicast groups. The command is included when you [install p4net](getting-started/installation.md).

## Starting the shell

The topology file must define a module-level `topology` variable containing a `Topology` instance. It may also define `setup(net)`, which runs after the network starts and before the shell opens. The [IPv4 forwarding example](examples/l3-forwarding.md) shows both definitions.

Each file is loaded as a separate Python module. It can define helper classes and dataclasses for use when constructing the topology or running `setup(net)`.

After [installing the CLI with pipx](getting-started/installation.md#install-cli), start the topology with the following command.

```bash
sudo p4net topology.py
```

In interactive mode, `Ready` appears after the network starts and `setup(net)` finishes. On exit, `Stopping topology...` appears before cleanup starts.

```text
Starting topology.py...
Ready (2 hosts, 1 switch). Type 'help' for commands.

p4net> exit
Stopping topology...
```

The CLI accepts the following arguments.

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

### Using `sudo` with a virtual environment

If you installed p4net in a virtual environment, use the [entry point for that project environment](getting-started/installation.md#project-environments).

The examples below use hosts `h1`, `h2` and switch `s1`. IPv6 commands use hosts configured with IPv6 addresses. Table names, actions, counters, and controller metadata come from the loaded P4 program. Enter each `p4net>` command on one line.

## Top-level commands

### `help` and `help <command>`

`help` groups commands into general, host, and switch commands. Give a topic to see its syntax and description.

```text
p4net> help switches
switches
  List switches and their gRPC addresses / pids.

p4net>
```

Use a host or switch name from the topology to see usage for that node, for example `help h1 ping` or `help s1 table add`. Use `help <host> ping` or `help <switch> table add` for the generic syntax. Press `Tab` after `help` to complete a topic.

### `exit`, `quit`

Leave the shell and shut down the network. `Ctrl-D` on an empty input line does the same.

### `status`

Show whether the network is running, the host and switch counts, and the BMv2 log directory.

### `hosts`

List each host's first configured IPv4 and IPv6 addresses, followed by its interfaces. Addresses retain their prefix lengths. An unconfigured address is shown as `-`.

```text
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  fd00::1/64   h1-eth0
h2    10.0.0.2/24  fd00::2/64   h2-eth0
```

### `switches`

List each switch's gRPC address, BMv2 process PID, and log file path.

### `pingall [count] [timeout]`

Run IPv4 ping between hosts with a configured primary IPv4 address, in both directions. The matrix includes all hosts, with the row as source and column as destination. `count` defaults to `1` and `timeout` to `2.0` seconds. Both follow the [ping argument rules](#ping--count-timeout).

Cells show `1` when at least one probe received a reply, `X` when none did, `-` for the same host, and `?` for an untested pair. To measure packet loss between two hosts, run [`<host> ping`](#ping--count-timeout) with multiple probes.

The shell reports progress in batches when there are at least three eligible hosts or when each pair uses multiple probes. Press `Ctrl-C` to show the results collected so far. Pending pairs appear as `?`, and the success summary counts only completed pairs.

```text
p4net> pingall 2 2.0
Pinging 2 host pairs...
1/2 host pairs checked
H \ H  h1   h2
h1     -    1
h2     1    -
2/2 succeeded
```

### `pingall6 [count] [timeout]`

Run IPv6 ping between hosts with a configured primary IPv6 address. Hosts without one are excluded. The argument rules and output format match `pingall`. If no host has IPv6 configured, the result is `(no IPv6-equipped hosts in topology)`.

```text
p4net> pingall6
H \ H  h1   h2
h1     -    1
h2     1    -
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

p4net calculates the overall wait from the count, timeout, and default one-second send interval. This wait must not exceed 2,147,483 seconds. A single probe displays `OK` if it receives a reply and `FAIL` otherwise.

For multiple probes, the shell displays each reply's sequence number and round-trip time as it arrives, then reports how many probes were sent and received and the loss percentage. Press `Ctrl-C` to end a run early and show any available statistics for the probes already sent.

```text
p4net> h1 ping h2
OK

p4net> h1 ping h2 2 2
PING h2 (10.0.0.2)
seq=1  time=0.34 ms
seq=2  time=0.28 ms
2 sent, 2 received, 0% loss
```

### `<host> ping6 <target> [count] [timeout]`

Run IPv6 ping with the same defaults and output as `ping`. A named target requires a primary IPv6 address. You can also give an IPv6 address directly.

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

The shell displays stdout and stderr as the command writes and flushes them. Stderr lines start with `[stderr]`, and a nonzero exit status adds `[exit N]`. Press `Ctrl-C` to cancel. On cancellation or timeout, the shell stops the command's process group before returning to the prompt.

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

The heading shows the table name and entry count. Each match field and action parameter appears on its own line as `name=value`. Values are formatted by field width: 32-bit values as IPv4 addresses, 128-bit values as IPv6 addresses, 48-bit values as MAC addresses, and other widths as decimal numbers. Match types add the prefix length, mask, or range.

For a table containing one IPv4 forwarding entry:

```text
p4net> s1 table dump MyIngress.ipv4_lpm
MyIngress.ipv4_lpm (1 entry)
#0
  match:    hdr.ipv4.dstAddr=10.0.0.1/32
  action:   MyIngress.set_egress_port
  params:   port=1
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

For a nonzero LPM prefix, bits outside the prefix must be zero. `10.0.0.5/24` reports an `EncodingError`. Use `10.0.0.0/24`. Prefix `/0` is a wildcard: its value is checked for format and width before the matching field is omitted.

For a nonzero ternary mask, value bits must be zero wherever the mask is zero. `0xab&&&0xf0` reports an `EncodingError`. Use `0xa0&&&0xf0`. A zero mask is a wildcard: both parts are checked for format and width before the matching field is omitted, even when the value is nonzero.

Range bounds must satisfy `low <= high`. Equal bounds match one value. Reversed bounds report an `EncodingError`.

For a 16-bit range field, `key=0..65535` matches every value. Such wildcard fields are omitted from entries shown by `table dump`; the table still requires a priority.

For exact/LPM-only tables, omit `priority:`. Tables with optional, ternary or range keys require it when inserting entries, including entries that omit an optional field as a wildcard. The priority must be an integer from `1` to `2147483647`. Zero, negative and out-of-range values report an `EncodingError` before sending the request. For a table with an exact destination MAC, a ternary source MAC, and a range of EtherTypes:

```text
p4net> s1 table add MyIngress.l2_match match: hdr.ethernet.dstAddr=00:00:00:00:00:02,hdr.ethernet.srcAddr=00:00:00:00:00:00&&&ff:ff:ff:ff:ff:00,hdr.ethernet.etherType=0x88b0..0x88bf action: MyIngress.set_egress_port params: port=2 priority: 17
ok
```

### `<switch> table del <table> match: <kv>... [priority: <n>]`

Delete an entry using its match key. Use the same match-value formats as `table add`, including `value&&&mask` and `low..high`. For tables with optional, ternary or range keys, supply the entry's original priority as `priority: N`, including for wildcard entries. It must be an integer from `1` to `2147483647`. Omitting a required priority reports an `EncodingError`. For exact/LPM-only tables, omit `priority:`.

```text
p4net> s1 table del MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.5/32
ok
```

### `<switch> table clear <table>`

Delete all entries from the table and return `cleared N entries`.

### `<switch> counter <name> [<index> | --all | --nonzero]`

Read an indirect counter. A specific index returns `pkts=N bytes=M`. With just the counter name or with `--all`, the table lists every returned cell in index order. If the switch returns no cells, the shell displays `(no populated cells)`.

Add `--nonzero` after the counter name to omit cells whose packet and byte counts are both zero. The shell reports how many cells were omitted. When all returned cells are zero, this view shows only the omission message.

Table columns are right-aligned.

Use `<switch> counter reset <name> [<index>]` to reset one cell. Omit the index to reset all cells returned by the switch. BMv2 reports both `pkts` and `bytes` and clears the fields selected by the counter unit in P4Info. `PACKETS` clears `pkts`, `BYTES` clears `bytes`, and `BOTH` clears both fields.

In a fresh [L3 forwarding topology](examples/l3-forwarding.md), `MyIngress.ingress_pkts` has unit `PACKETS`. One ping gives cell `1` a nonzero packet count. After the reset, `pkts` is zero and `bytes` stays at `98` in this run.

```text
p4net> h1 ping h2
OK

p4net> s1 counter MyIngress.ingress_pkts 1
pkts=1 bytes=98

p4net> s1 counter reset MyIngress.ingress_pkts 1
ok

p4net> s1 counter MyIngress.ingress_pkts 1
pkts=0 bytes=98
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

Display new `PacketIn` messages as they arrive. The shell prints the received count when the target is reached or the total timeout elapses. Press `Ctrl-C` to stop listening and see how many packets were displayed. Defaults are `count=1` and `timeout=10.0` seconds. `count` must be a positive integer. `timeout` must be finite, non-negative, and within the system wait limit. With a positive timeout, the shell shows the switch, target count, and time limit before waiting. Use `timeout=0` to return immediately.

```text
p4net> s1 packet listen count=2 timeout=3
Waiting for 2 packets on s1 (up to 3s)...
[_pad0=0] [ingress_port=1] ffffffffffff000000000001080600010800060400010000000000010a000001...
1/2 packets received (timeout)
```

Each packet is shown on one line with `[name=value]` metadata fields sorted by name, followed by the hex payload. Hex payloads longer than 64 characters are truncated with `...`.

## Keyboard shortcuts

- `Ctrl-C`: cancel the current input line or command. The topology keeps running.
- `Ctrl-D` on an empty line: leave the shell and shut down the network.
- `↑` / `↓`: browse history, stored in `~/.p4net_history`.
- `Tab`: complete top-level commands, host and switch names, and node verbs such as `ping` or `table`.
