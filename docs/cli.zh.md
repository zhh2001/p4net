---
description: p4net 命令行参考，介绍拓扑启动、连通性测试、主机命令、表项操作和控制器包收发。
---

# CLI 参考

`p4net` 命令启动拓扑后会进入交互式命令行，可在其中执行主机命令、测试连通性、配置交换机表项、读取计数器和管理组播组。[安装 p4net](getting-started/installation.md) 后即可使用该命令。

## 启动命令行 { #启动-shell }

拓扑文件必须定义模块级变量 `topology`，其值为 `Topology` 实例。文件还可以定义 `setup(net)`，在网络启动后、进入交互命令行前执行。[IPv4 转发示例](examples/l3-forwarding.md) 包含这两部分。

每个文件作为独立的 Python 模块加载，可以定义辅助类和 dataclass，用于构建拓扑或执行 `setup(net)`。

[通过 pipx 安装 CLI](getting-started/installation.md#install-cli) 后，可以直接启动拓扑。

```bash
sudo p4net topology.py
```

在交互模式下，网络启动且 `setup(net)` 执行完成后显示 `Ready`。退出时先显示 `Stopping topology...`，再清理网络资源。

```text
Starting topology.py...
Ready (2 hosts, 1 switch). Type 'help' for commands.

p4net> exit
Stopping topology...
```

启动命令接受以下参数。

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

如果 p4net 安装在虚拟环境中，请使用[该项目环境中的命令入口](getting-started/installation.md#project-environments)。

以下示例使用主机 `h1`、`h2` 和交换机 `s1`。IPv6 命令需要主机配置 IPv6 地址。表名、动作名、计数器和控制器元数据由加载的 P4 程序决定。`p4net>` 后的每条命令均在一行内输入。

## 网络命令 { #顶级命令 }

### `help` 与 `help <command>`

`help` 按通用、主机和交换机三组列出命令。指定主题后显示对应语法和说明。

```text
p4net> help switches
switches
  List switches and their gRPC addresses / pids.

p4net>
```

可以用当前拓扑中的节点名查询用法，例如 `help h1 ping` 或 `help s1 table add`。用 `help <host> ping` 或 `help <switch> table add` 可查看通用语法。在 `help` 后按 `Tab` 可补全帮助主题。

### `exit`、`quit`

退出命令行并关闭网络。在空输入行上按 `Ctrl-D` 效果相同。

### `status`

显示网络是否运行、主机和交换机数量，以及 BMv2 日志目录。

### `hosts`

列出每台主机配置的首个 IPv4 地址、首个 IPv6 地址及接口名。地址保留前缀长度，未配置的地址以 `-` 表示。

```text
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  fd00::1/64   h1-eth0
h2    10.0.0.2/24  fd00::2/64   h2-eth0
```

### `switches`

列出每台交换机的 gRPC 地址、BMv2 进程 PID 和日志文件路径。

### `pingall [count] [timeout]`

在配置了主 IPv4 地址的主机之间逐方向执行 ping。矩阵列出全部主机，行表示源主机，列表示目标主机。`count` 默认 `1`，`timeout` 默认 `2.0` 秒，取值范围见 [ping 参数说明](#ping--count-timeout)。

`1` 表示至少收到一次应答，`X` 表示未收到应答，`-` 表示自身，`?` 表示未测得结果的方向。要测量两台主机之间的丢包率，可用 [`<host> ping`](#ping--count-timeout) 发送多次探测。

参与测试的主机达到三台，或每个方向进行多次探测时，终端分批显示进度。按 `Ctrl-C` 可查看已完成的结果，尚未完成的方向显示为 `?`，成功数只按已完成的方向统计。

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

在配置了主 IPv6 地址的主机之间执行 IPv6 ping，排除未配置 IPv6 的主机。参数规则和输出格式与 `pingall` 相同。如果没有主机配置 IPv6，返回 `(no IPv6-equipped hosts in topology)`。

```text
p4net> pingall6
H \ H  h1   h2
h1     -    1
h2     1    -
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

p4net 根据次数、超时和默认一秒的发送间隔计算整体等待时限，上限为 2,147,483 秒。单次探测收到应答时显示 `OK`，否则显示 `FAIL`。

多次探测时，每收到一次应答就显示序号和往返时延，结束时汇总发送数、收到数和丢包率。按 `Ctrl-C` 可提前结束，并显示已发送探测的可用统计。

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

执行 IPv6 ping，默认参数和输出格式与 `ping` 相同。使用主机名时，目标主机须有主 IPv6 地址，也可以直接指定 IPv6 地址。

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

命令刷新 stdout 或 stderr 时，终端实时显示输出。stderr 各行带 `[stderr]` 前缀，退出状态非零时在末尾显示 `[exit N]`。按 `Ctrl-C` 可取消。取消或超时后，终端会停止本次命令的进程组，再返回提示符。

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

表头显示表名和表项数。匹配字段和动作参数按 `名称=值` 逐行显示。值按字段位宽格式化：32 位显示为 IPv4 地址，128 位显示为 IPv6 地址，48 位显示为 MAC 地址，其他位宽显示为十进制数。匹配类型决定是否附带前缀长度、掩码或范围。

表中只有一条 IPv4 转发表项时，输出如下：

```text
p4net> s1 table dump MyIngress.ipv4_lpm
MyIngress.ipv4_lpm (1 entry)
#0
  match:    hdr.ipv4.dstAddr=10.0.0.1/32
  action:   MyIngress.set_egress_port
  params:   port=1
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

LPM 前缀长度非零时，前缀外的位必须为零。例如 `10.0.0.5/24` 会显示 `EncodingError`，应使用 `10.0.0.0/24`。`/0` 表示通配，值仍需通过格式和位宽校验，再省略该匹配字段。

ternary 掩码非零时，掩码为零的位置，值也必须为零。例如 `0xab&&&0xf0` 会显示 `EncodingError`，应使用 `0xa0&&&0xf0`。掩码全零表示通配，值和掩码仍需通过格式和位宽校验，再省略该匹配字段，值非零也一样。

range 的下界不能大于上界。上下界相等时匹配单个值，顺序颠倒时显示 `EncodingError`。

16 位 range 字段可用 `key=0..65535` 匹配全部值。`table dump` 不显示这样的通配字段，表仍需要优先级。

仅含 exact/LPM 字段的表不填写 `priority:`，含 optional、ternary 或 range 字段的表在插入时需要优先级，省略 optional 字段作通配匹配的条目也一样。优先级必须是 `1` 到 `2147483647` 之间的整数，零、负数和越界值会在发送请求前报 `EncodingError`。下面的表按目标 MAC 做 exact 匹配、按源 MAC 做 ternary 匹配，并对 EtherType 做 range 匹配：

```text
p4net> s1 table add MyIngress.l2_match match: hdr.ethernet.dstAddr=00:00:00:00:00:02,hdr.ethernet.srcAddr=00:00:00:00:00:00&&&ff:ff:ff:ff:ff:00,hdr.ethernet.etherType=0x88b0..0x88bf action: MyIngress.set_egress_port params: port=2 priority: 17
ok
```

### `<switch> table del <table> match: <kv>... [priority: <n>]` { #table-del--match- }

按匹配键删除表项，匹配值格式与 `table add` 相同，也使用 `值&&&掩码` 和 `下界..上界`。含 optional、ternary 或 range 字段的表必须用 `priority: N` 指定条目的原优先级，通配条目也一样。优先级必须是 `1` 到 `2147483647` 之间的整数。缺少必需的优先级时显示 `EncodingError`。仅含 exact/LPM 字段的表不填写 `priority:`。

```text
p4net> s1 table del MyIngress.ipv4_lpm match: hdr.ipv4.dstAddr=10.0.0.5/32
ok
```

### `<switch> table clear <table>`

删除表中所有表项，返回 `cleared N entries`。

### `<switch> counter <name> [<index> | --all | --nonzero]` { #counter-reset-- }

读取间接计数器。指定索引时返回 `pkts=N bytes=M`。仅指定计数器名称或加上 `--all` 时，按索引顺序列出交换机返回的全部单元。交换机没有返回单元时显示 `(no populated cells)`。

在计数器名称后加 `--nonzero`，可省略包数和字节数都为零的单元。输出会注明省略数量。所有返回的单元均为零时，精简视图只显示省略提示。

表头和数值右对齐。

使用 `<switch> counter reset <name> [<index>]` 重置指定单元，省略索引则重置交换机返回的所有单元。BMv2 同时返回 `pkts` 和 `bytes`，并按 P4Info 中的计数单位清零对应字段。`PACKETS` 清零 `pkts`，`BYTES` 清零 `bytes`，`BOTH` 清零两个字段。

在刚启动的 [L3 转发拓扑](examples/l3-forwarding.md) 中，`MyIngress.ingress_pkts` 的计数单位是 `PACKETS`。执行一次 `ping` 后，索引 `1` 的包数变为非零。以下实测中，重置后 `pkts` 归零，`bytes` 保持为 `98`。

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

实时显示新到达的 `PacketIn`。收满目标包数或达到总超时后，终端汇总收到的包数。按 `Ctrl-C` 可停止监听，终端会汇总已显示的包数。默认 `count=1`、`timeout=10.0` 秒。`count` 必须为正整数，`timeout` 必须有限、非负且不超过系统等待上限。超时大于零时，终端在等待前显示交换机、目标包数和等待上限。设置 `timeout=0` 可立即返回。

```text
p4net> s1 packet listen count=2 timeout=3
Waiting for 2 packets on s1 (up to 3s)...
[_pad0=0] [ingress_port=1] ffffffffffff000000000001080600010800060400010000000000010a000001...
1/2 packets received (timeout)
```

每个包输出一行，先按名称排序显示 `[name=value]` 元数据字段，再显示十六进制内容。十六进制内容超过 64 个字符时截断并追加 `...`。

## 键盘快捷键

- `Ctrl-C`：取消当前输入行或正在执行的命令，拓扑继续运行。
- 空行上的 `Ctrl-D`：退出命令行并关闭网络。
- `↑` / `↓`：浏览历史记录，历史保存在 `~/.p4net_history`。
- `Tab`：补全网络命令、主机名、交换机名，以及 `ping`、`table` 等节点命令。
