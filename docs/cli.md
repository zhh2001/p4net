# CLI reference

This page documents `p4net`, the interactive shell installed by
`pip install -e '.[dev]'`. The shell wraps a `Network` and exposes
host commands, table programming, counter reads, and multicast control
without writing Python.

## Starting the shell

```
sudo p4net <topology.py>
sudo python -m p4net <topology.py>
```

Both forms accept the same flags:

```
usage: p4net [-h] [--no-shell] [--unsafe] [--log-dir LOG_DIR]
             [--pcap-dir PCAP_DIR] [--extra-compile-arg ARG]
             topology_file
```

- `--no-shell` — bring the network up, run `setup(net)` if defined,
  block until `SIGINT`, then tear down. Useful for scripted lab setups.
- `--unsafe` — skip `Topology.validate()`. Only use if you know your
  topology is well-formed.
- `--log-dir DIR` — directory for BMv2 log files. Defaults to a fresh
  tempdir.
- `--pcap-dir DIR` — per-port BMv2 packet captures. Disabled by default.
- `--extra-compile-arg ARG` — extra argument forwarded to `p4c`,
  repeatable.

### Note on `sudo` and `PATH`

`sudo` strips the venv from `PATH` by default (`secure_path` in
`/etc/sudoers`). If `sudo p4net ...` reports "command not found", run
it via the venv-resolved binary:

```
sudo env "PATH=$PATH" p4net <topology.py>
```

or install p4net system-wide so the binary lives on `secure_path`.

## Top-level commands

### `help` and `help <command>`

Lists every command, or prints the usage and description of one:

```
p4net> help
Commands
  help                   List commands or show details for one.
  exit                   Exit the shell.
  ...
p4net> help pingall
pingall [count] [timeout]

Ping every pair of hosts; print a result matrix.
```

### `exit`, `quit`

Tear down the network and exit. `Ctrl-D` on an empty line is equivalent.

### `status`

Print the network's running state, host/switch counts, and log directory.

### `hosts`

List every host with its primary IP and interfaces.

### `switches`

List every switch with its gRPC address and BMv2 PID.

### `pingall [count] [timeout]`

Ping every pair of hosts and print a result matrix. `count` defaults to
1, `timeout` to 1.0 seconds. Cells: `1` succeeded, `X` failed, `-` self,
`?` unknown.

```
p4net> pingall 2 2.0
H \ H   h1   h2
   h1    -    1
   h2    1    -
2/2 succeeded
```

## Host commands

### `<host> ping <target> [count] [timeout]`

Ping `<target>` from `<host>`. `<target>` is resolved as a host name
first (using its primary IP); any string that does not match a host name
is forwarded verbatim to `iputils-ping`. Defaults: `count=1`,
`timeout=1.0`.

```
p4net> h1 ping h2
ok
p4net> h1 ping 8.8.8.8 1 0.5
fail
```

### `<host> cmd <argv>...`

Run a command inside the host's namespace. Quoting follows shell rules
(`shlex`). Stdout is rendered first, stderr lines are prefixed with
`[stderr]`, and a non-zero exit code is reported with `[exit N]`.

```
p4net> h1 cmd ip -br addr
lo               UNKNOWN        127.0.0.1/8
h1-eth0          UP             10.0.0.1/24
```

### `<host> ifconfig`

Shorthand for `<host> cmd ip -br addr`.

## Switch commands

### `<switch> log`

Print the absolute path of the switch's BMv2 log file.

### `<switch> table list`

List the switch's tables and their match-key types.

```
p4net> s1 table list
MyIngress.ipv4_lpm
  hdr.ipv4.dstAddr (lpm, 32 bits)
```

### `<switch> table dump <table>`

Print every entry in the named table. Match values are decoded via
`P4InfoIndex.decode_match` into width-aware human strings (IPv4 for
32-bit fields, MAC for 48-bit, decimal otherwise).

```
p4net> s1 table dump MyIngress.ipv4_lpm
#0
  table:    MyIngress.ipv4_lpm
  match:    {'hdr.ipv4.dstAddr': '10.0.0.1/32'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
```

### `<switch> table add <table> match: <kv>... action: <name> [params: <kv>...] [priority: <n>]`

Insert a table entry. Section markers (`match:`, `action:`, `params:`,
`priority:`) introduce each field; key=value pairs within a section may
be comma-separated and may have spaces around the commas. A full
worked example:

```
p4net> s1 table add MyIngress.ipv4_lpm \
         match: hdr.ipv4.dstAddr=10.0.0.5/32 \
         action: MyIngress.set_egress_port \
         params: port=2
ok
```

For ternary or range tables, supply a `priority:` section. Tables
without ternary/range fields reject `priority:`.

### `<switch> table del <table> match: <kv>...`

Delete an entry by match key. The `match:` syntax matches `table add`.

### `<switch> table clear <table>`

Delete every entry from a table. Returns the count deleted.

### `<switch> counter [reset] <name> [<index>]`

Read or zero a counter cell. Without `reset`, prints `pkts=N bytes=M`
for the given index (or every populated cell if no index is given).
With `reset`, zeroes the cell instead.

```
p4net> s1 counter MyIngress.ingress_pkts 0
pkts=2 bytes=196
p4net> s1 counter reset MyIngress.ingress_pkts 0
ok
```

### `<switch> mcast list`

List every multicast group as `<id>: [<port>, ...]`.

### `<switch> mcast add <id> <port,port,...>`

Create a multicast group with the given replication ports.

```
p4net> s1 mcast add 1 1,2,3
ok
```

### `<switch> mcast del <id>`

Delete a multicast group.

## Keyboard shortcuts

- `Ctrl-C` — cancel the current input. If a command is mid-execution,
  the underlying process group is signalled and the shell returns to
  the prompt.
- `Ctrl-D` on an empty line — exit cleanly (same as `exit` / `quit`).
- `↑` / `↓` — walk command history (persisted in `~/.p4net_history`).
- `Tab` — completion on top-level commands, host names, switch names,
  and their sub-verbs.
