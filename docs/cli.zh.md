---
description: p4net 命令行参考，介绍拓扑启动、连通性测试、主机命令、表项操作和控制器包收发。
---

# CLI 参考

`p4net` 命令启动拓扑后会进入交互式命令行，可在其中执行主机命令、测试连通性、配置交换机表项、读取计数器和管理组播组。[安装 p4net](getting-started/installation.md) 后即可使用该命令。

## 启动命令行 { #启动-shell }

拓扑文件必须定义模块级变量 `topology`，其值为 `Topology` 实例。文件还可以定义 `setup(net)`，在网络启动后、进入交互命令行前执行。[IPv4 转发示例](examples/l3-forwarding.md) 包含这两部分。

每个文件作为独立的 Python 模块加载，可以定义辅助类和 dataclass，用于构建拓扑或执行 `setup(net)`。

使用安装了 p4net 的 Python 环境启动，两种入口等效：

```bash
sudo env "PATH=$PATH" p4net topology.py
sudo env "PATH=$PATH" python -m p4net topology.py
```

两种入口接受相同的参数：

```text
usage: p4net [-h] [--no-shell] [--unsafe] [--log-dir LOG_DIR]
             [--pcap-dir PCAP_DIR] [--extra-compile-arg ARG] [-v]
             topology_file
```

| 参数 | 作用 |
| --- | --- |
| `-h`、`--help` | 显示启动参数说明并退出。 |
| `--no-shell` | 启动网络、运行 `setup(net)`（如有），等待 `SIGINT` 或 `SIGTERM` 后关闭网络。 |
| `--unsafe` | 启动前跳过 `Topology.validate()`。 |
| `--log-dir DIR` | 将 BMv2 日志写入 `DIR`，默认新建临时目录。 |
| `--pcap-dir DIR` | 将 BMv2 按端口抓取的数据包保存到 `DIR`，不指定时不抓包。 |
| `--extra-compile-arg ARG` | 向 `p4c` 传递额外参数，可重复指定。值以 `-` 开头时使用 `--extra-compile-arg=-DDEBUG` 这样的形式。 |
| `-v`、`--verbose` | 将日志级别设为 INFO，`-vv` 设为 DEBUG，默认级别为 WARNING。 |

指定 `--pcap-dir` 后，可在 `Topology.add_switch()` 中设置 `pcap_enabled=False`，关闭某台交换机的抓包。

### 虚拟环境与 `sudo` { #关于-sudo-与-path }

如果系统的 `sudo` 用 `secure_path` 替换了 `PATH`，它可能找不到当前环境中的 `p4net`、Python 或外部工具。上面的启动命令通过 `env` 传入当前 `PATH`，使两个入口都使用当前环境。

以下示例使用主机 `h1`、`h2` 和交换机 `s1`。IPv6 命令需要主机配置 IPv6 地址。表名、动作名、计数器和控制器元数据由加载的 P4 程序决定。`p4net>` 后的每条命令均在一行内输入。

## 网络命令 { #顶级命令 }

### `help` 与 `help <command>`

`help` 列出可用命令，指定主题后显示对应语法和说明：

```text
p4net> help pingall
pingall [count] [timeout]

Ping every pair of hosts; print a result matrix.
```

查询节点命令的帮助时，使用字面占位名，例如 `help <host> ping` 或 `help <switch> table add`。

### `exit`、`quit`

退出命令行并关闭网络。在空输入行上按 `Ctrl-D` 效果相同。

### `status`

显示网络是否运行、主机和交换机数量，以及 BMv2 日志目录。

### `hosts`

列出每台主机配置的首个 IPv4 地址、首个 IPv6 地址及接口名。地址保留前缀长度，未配置的地址以 `-` 表示。

```text
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  fd00::1/64  h1-eth0
h2    10.0.0.2/24  fd00::2/64  h2-eth0
```

### `switches`

列出每台交换机的 gRPC 地址、BMv2 进程 PID 和日志文件路径。

### `pingall [count] [timeout]`

在配置了主 IPv4 地址的主机之间逐方向执行 ping。矩阵列出全部主机，行表示源主机，列表示目标主机。`count` 默认 `1`，`timeout` 默认 `2.0` 秒，取值范围见 [ping 参数说明](#ping--count-timeout)。`1` 表示成功，`X` 表示失败，`-` 表示自身。未配置 IPv4 的主机所涉及的方向不执行测试，以 `?` 表示。

```text
p4net> pingall 2 2.0
H \ H h1  h2
h1  -   1
h2  1   -
2/2 succeeded
```

### `pingall6 [count] [timeout]`

在配置了主 IPv6 地址的主机之间执行 IPv6 ping，排除未配置 IPv6 的主机。参数规则和输出格式与 `pingall` 相同。如果没有主机配置 IPv6，返回 `(no IPv6-equipped hosts in topology)`。

```text
p4net> pingall6
H \ H h1  h2
h1  -   1
h2  1   -
2/2 succeeded
```

### `topology graph [path] [layout=LR|TB|BT|RL] [format=png|svg|pdf|dot]`

校验拓扑并生成图。不指定 `path` 时显示 DOT 源码，指定路径时按所选格式写入文件，并返回文件的绝对路径。`layout` 设置 Graphviz 的 `rankdir`，默认 `LR`，`format` 默认 `png`。

`format=dot` 直接写入源码，其他格式需要系统安装 `dot`。

```text
p4net> topology graph /tmp/topo.svg layout=TB format=svg
/tmp/topo.svg
```

## 主机命令

### `<host> ping <target> [count] [timeout]`

从指定主机发起 ping。目标是拓扑中的主机名时，解析为该主机的主 IPv4 地址。其他目标直接传给 `ping`，字符串中包含 `:` 时使用 IPv6。按主机名发起 IPv6 ping 使用 `ping6`。

默认 `count=1`、`timeout=2.0` 秒。`count` 必须为正整数，表示回显请求的次数上限。`timeout` 必须有限且非负，可以使用小数秒，例如 `0.25`，并传给 `ping -W`。超时值为 `0` 时，由进程的整体等待时限控制退出。

p4net 根据次数、超时和默认一秒的发送间隔计算整体等待时限，上限为 2,147,483 秒。收到至少一次应答就显示 `OK`，部分丢包也算成功。没有应答或执行失败时显示 `FAIL`。

```text
p4net> h1 ping h2
OK
p4net> h1 ping 10.0.0.2 2 2
OK
```

### `<host> ping6 <target> [count] [timeout]`

执行 IPv6 ping，默认参数与 `ping` 相同。主机名解析为该主机的主 IPv6 地址，未配置时会报错，也可以直接指定 IPv6 地址。

```text
p4net> h1 ping6 h2
OK
p4net> h1 ping6 fd00::2
OK
```

### `<host> xterm`

在主机命名空间内打开运行 `bash` 的 `xterm`。需要安装 `xterm`，并能通过 `DISPLAY` 连接图形会话。网络会跟踪该进程，并在关闭时终止它。

```text
p4net> h1 xterm
xterm spawned (pid=12345)
```

PID 随实际进程变化。

### `<host> cmd <argv>...`

在主机命名空间内执行命令，超时为 30 秒。引号按 `shlex` 规则将文本组合为参数，随后直接执行命令。需要管道或重定向等 Shell 功能时，使用 `sh -c '...'`。

先显示 stdout，再逐行显示带 `[stderr]` 前缀的 stderr。退出状态非零时，末尾追加 `[exit N]`。

```text
p4net> h1 cmd printf 'hello\n'
hello
```

### `<host> ifconfig`

在主机命名空间内执行 `ip -br addr`，显示其 stdout。

## 交换机命令

### `<switch> log`

显示交换机的 BMv2 日志文件路径。`--log-dir` 使用相对路径时，这里也显示相对路径。

### `<switch> table list`

列出已加载 P4Info 中的表，每行显示表名，以及匹配字段的类型和名称：

```text
p4net> s1 table list
MyIngress.ipv4_lpm  (lpm: hdr.ipv4.dstAddr)
```

### `<switch> table dump <table>`

显示指定表中的表项。值按字段位宽格式化：32 位显示为 IPv4 地址，128 位显示为 IPv6 地址，48 位显示为 MAC 地址，其他位宽显示为十进制数。匹配类型决定是否附带前缀长度、掩码或范围，动作参数也按位宽格式化。

表中只有一条 IPv4 转发表项时，输出如下：

```text
p4net> s1 table dump MyIngress.ipv4_lpm
#0
  table:    MyIngress.ipv4_lpm
  match:    {'hdr.ipv4.dstAddr': '10.0.0.1/32'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
```

空表返回 `(table 'MyIngress.ipv4_lpm' is empty)`。

### `<switch> table add <table> match: <kv>... action: <name> [params: <kv>...] [priority: <n>]`

插入表项。`match:`、`action:`、`params:` 和 `priority:` 分别标记各部分。匹配字段和动作参数用 `key=value` 表示，多项之间可用空格或逗号分隔。

```text
p4net> s1 table add MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.5/32 action: MyIngress.set_egress_port params: port=2
ok
```

匹配值的格式由目标交换机 P4Info 中的字段类型决定：

| 匹配类型 | 示例 |
| --- | --- |
| exact、optional | `key=123` |
| lpm | `key=10.0.0.0/24` |
| ternary | `key=0x1200&&&0xff00` |
| range | `key=100..200` |

各部分可以使用字段支持的值格式，包括十进制或十六进制整数、IP 地址和 MAC 地址。组合值内不加空格，需要空格时用引号包住整个值。

仅含 exact/LPM 字段的表不填写 `priority:`，含 optional、ternary 或 range 字段的表在插入时需要优先级，省略 optional 字段作通配匹配的条目也一样。下面的表按目标 MAC 做 exact 匹配、按源 MAC 做 ternary 匹配，并对 EtherType 做 range 匹配：

```text
p4net> s1 table add MyIngress.l2_match match: hdr.ethernet.dstAddr=00:00:00:00:00:02,hdr.ethernet.srcAddr=00:00:00:00:00:00&&&ff:ff:ff:ff:ff:00,hdr.ethernet.etherType=0x88b0..0x88bf action: MyIngress.set_egress_port params: port=2 priority: 17
ok
```

### `<switch> table del <table> match: <kv>... [priority: <n>]` { #table-del--match- }

按匹配键删除表项，匹配值格式与 `table add` 相同，也使用 `值&&&掩码` 和 `下界..上界`。条目带有优先级时，同时用 `priority: N` 指定原值。

```text
p4net> s1 table del MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.5/32
ok
```

### `<switch> table clear <table>`

删除表中所有表项，返回 `cleared N entries`。

### `<switch> counter [reset] <name> [<index>]`

读取间接计数器，或用 `reset` 清零。指定索引时，读取结果为 `pkts=N bytes=M`。不指定索引时，以表格列出返回的计数单元，没有结果则显示 `(no populated cells)`。实际记录包数还是字节数由 P4 计数器类型决定，不指定索引的清零操作会清空所有返回的单元。

```text
p4net> s1 counter reset MyIngress.ingress_pkts 1
ok
p4net> s1 counter MyIngress.ingress_pkts 1
pkts=0 bytes=0
```

### `<switch> mcast list`

按组 ID 顺序列出组播组，格式为 `<id>: [<port>, ...]`，没有组时显示 `(no multicast groups)`。

### `<switch> mcast add <id> <port,port,...>`

创建组播组，指定复制端口。

```text
p4net> s1 mcast add 1 1,2,3
ok
```

### `<switch> mcast del <id>`

删除指定的组播组。

### `<switch> packet send <hex_payload> [metadata: <k>=<v>[,<k>=<v>...]]`

将 `PacketOut` 放入 P4Runtime StreamChannel 的发送队列。载荷是完整数据包的十六进制字符串，元数据名称和位宽来自 P4Info 中的 `packet_out` 头，未提供的字段填零。

[CPU punt 示例](examples/cpu-punt.md) 提供了控制器包头和 CPU 端口配置，可使用以下命令发送 PacketOut：

```text
p4net> s1 packet send ffffffffffff000000000001880b48656c6c6f metadata: egress_port=1
ok
```

### `<switch> packet listen [count=N] [timeout=T]`

收集命令运行期间新到达的 `PacketIn`。收到 `count` 个包或达到总超时后返回，默认 `count=1`、`timeout=10.0` 秒。`count` 必须为正整数，`timeout` 必须有限、非负且不超过系统等待上限，`0` 表示不等待。超时时会显示已经收集到的包。

```text
p4net> s1 packet listen
p4net> s1 packet listen count=2 timeout=3
```

每个包输出一行，先按名称排序显示 `[name=value]` 元数据字段，再显示十六进制内容。十六进制内容超过 64 个字符时截断并追加 `...`。没有包到达时，返回 `(no packets within 3.0s)`。

## 键盘快捷键

- `Ctrl-C`：取消正在编辑的输入行。
- 空行上的 `Ctrl-D`：退出命令行并关闭网络。
- `↑` / `↓`：浏览历史记录，历史保存在 `~/.p4net_history`。
- `Tab`：补全网络命令、主机名、交换机名，以及 `ping`、`table` 等节点命令。
