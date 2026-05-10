---
description: p4net 交互式 Shell 的命令参考——每条顶级、主机、交换机命令的语法说明与示例输出。
---

# CLI 参考

本页记录 `p4net` 交互式 Shell——通过 `pip install -e '.[dev]'`
安装的命令。Shell 包装一个 `Network`，提供主机命令、表项编程、
计数器读取、组播控制等功能，无需写 Python 代码。

## 启动 Shell

```
sudo p4net <topology.py>
sudo python -m p4net <topology.py>
```

两种形式接受相同的参数：

```
usage: p4net [-h] [--no-shell] [--unsafe] [--log-dir LOG_DIR]
             [--pcap-dir PCAP_DIR] [--extra-compile-arg ARG]
             topology_file
```

- `--no-shell` —— 拉起网络、运行 `setup(net)`（如有）、阻塞至
  `SIGINT`、然后清理。适用于脚本化实验环境。
- `--unsafe` —— 跳过 `Topology.validate()`。仅在你确定拓扑没有
  问题时使用。
- `--log-dir DIR` —— BMv2 日志目录。默认是新建的临时目录。
- `--pcap-dir DIR` —— 按端口的 BMv2 抓包目录。默认禁用。
- `--extra-compile-arg ARG` —— 额外传给 `p4c` 的参数，可以多次
  指定。

### 关于 `sudo` 与 `PATH`

`sudo` 默认会通过 `secure_path`（在 `/etc/sudoers` 中定义）剥离
虚拟环境的 `PATH`。如果 `sudo p4net ...` 报 "command not found"，
显式指定二进制路径：

```
sudo env "PATH=$PATH" p4net <topology.py>
```

或者把 p4net 装到系统级目录，让二进制文件落在 `secure_path` 中。

## 顶级命令

### `help` 与 `help <command>`

列出所有命令，或打印某条命令的语法与说明：

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

### `exit`、`quit`

拆掉网络并退出。在空行上按 `Ctrl-D` 等价。

### `status`

打印网络运行状态、主机/交换机数量、日志目录。

### `hosts`

列出每台主机的主 IPv4、主 IPv6、接口列表。没有对应地址的列以
`-` 表示。

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  fd00::1/64   h1-eth0
h2    10.0.0.2/24  -            h2-eth0
```

### `switches`

列出每台交换机的 gRPC 地址与 BMv2 进程 PID。

### `pingall [count] [timeout]`

对每一对主机执行 ping，打印结果矩阵。`count` 默认 1，`timeout`
默认 1.0 秒。单元格含义：`1` 成功、`X` 失败、`-` 自身、`?` 未知。

```
p4net> pingall 2 2.0
H \ H   h1   h2
   h1    -    1
   h2    1    -
2/2 succeeded
```

### `pingall6 [count] [timeout]`

对设置了 `primary_ip6` 的主机两两执行 IPv6 ping，渲染矩阵格式
与 `pingall` 一致。没有 IPv6 的主机被静默忽略。如果没有任何主机
配置了 v6 地址，返回 `(no IPv6-equipped hosts in topology)`。

```
p4net> pingall6
H \ H   h1   h2
   h1    -    1
   h2    1    -
2/2 succeeded
```

### `topology graph [path] [layout=LR|TB|BT|RL] [format=png|svg|pdf|dot]`

把拓扑渲染为 Graphviz DOT 图。

- 不带 `path`：把 DOT 源码打印到 stdout。
- 带 `path` 且 `format=dot`：直接把源码写入文件，不调用 `dot`
  （未安装 graphviz 时也能用）。
- 带 `path` 且其他 format：调用系统 `dot` 把图渲染到文件，返回
  渲染产物的绝对路径。

`layout` 控制 `rankdir`，默认 `LR`；`format` 默认 `png`。dispatcher
在渲染前调用 `Topology.validate()`，因此不合法的拓扑会得到清晰
错误，而不是产生看似正常但实则误导的图。

```
p4net> topology graph layout=TB
digraph p4net {
  rankdir=TB;
  node [fontname="monospace"];
  "h1" [shape=ellipse, label="h1\n10.0.0.1/24\nfd00::1/64"];
  "h2" [shape=ellipse, label="h2\n10.0.0.2/24"];
  "s1" [shape=box, label="s1\ngrpc :50051"];
  "h1" -> "s1" [arrowhead=none];
  "h2" -> "s1" [arrowhead=none];
}

p4net> topology graph /tmp/topo.png
/tmp/topo.png
```

## 主机命令

### `<host> ping <target> [count] [timeout]`

从 `<host>` 向 `<target>` 发 ping。`<target>` 优先按主机名解析
（取其主 IP）；不匹配任何主机名的字符串会原样传给
`iputils-ping`。默认 `count=1`、`timeout=1.0`。根据目标字符串
中是否含有 `:` 自动选择 IPv4 或 IPv6。如果两栈都有，需要强制
IPv6 时，使用 `<host> ping6`。

```
p4net> h1 ping h2
OK
p4net> h1 ping 8.8.8.8 1 0.5
FAIL
```

### `<host> ping6 <target> [count] [timeout]`

显式 IPv6 ping。`<target>` 可以是主机名（解析为该主机的
`primary_ip6`）或字面 IPv6 地址。如果目标是没有 `primary_ip6`
的主机名，会报错。

```
p4net> h1 ping6 h2
OK
p4net> h1 ping6 fd00::ff
FAIL
```

### `<host> xterm`

在主机命名空间内启动一个运行 `bash` 的 `xterm`。需要 `$DISPLAY`
已设置；否则报错。编排器跟踪所启动的进程，并在
`Network.stop()` 时把它终止。

```
p4net> h1 xterm
xterm spawned (pid=12345)
```

（输出来自一台已设置 `$DISPLAY` 的主机；测试套件不会跑这个例子，
因为 CI 没有 X server。）

### `<host> cmd <argv>...`

在主机命名空间内运行命令。引号遵循 Shell 规则（`shlex`）。
stdout 先打印；stderr 行加前缀 `[stderr]`；非零退出码以
`[exit N]` 形式追加在末尾。

```
p4net> h1 cmd ip -br addr
lo               UNKNOWN        127.0.0.1/8
h1-eth0          UP             10.0.0.1/24
```

### `<host> ifconfig`

`<host> cmd ip -br addr` 的简写。

## 交换机命令

### `<switch> log`

打印该交换机 BMv2 日志文件的绝对路径。

### `<switch> table list`

列出该交换机所有表及其匹配键类型。

```
p4net> s1 table list
MyIngress.ipv4_lpm
  hdr.ipv4.dstAddr (lpm, 32 bits)
```

### `<switch> table dump <table>`

打印指定表的所有表项。IPv4（32 位）与 IPv6（128 位）的 match
值以人类可读形式渲染（`10.0.0.1/24`、`fd00::/64`）；MAC（48 位）
使用规范冒号形式；其他位宽渲染为十进制整数。Action 参数也按位宽
解码：9 位的 `port` 显示为 `'2'`，而非 `b'\x02'`。

```
p4net> s1 table dump MyIngress.ipv4_lpm
#0
  table:    MyIngress.ipv4_lpm
  match:    {'hdr.ipv4.dstAddr': '10.0.0.1/32'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}

p4net> s1 table dump MyIngress.ipv6_lpm
#0
  table:    MyIngress.ipv6_lpm
  match:    {'hdr.ipv6.dstAddr': 'fd00::1/128'}
  action:   MyIngress.set_egress_port
  params:   {'port': '1'}
```

### `<switch> table add <table> match: <kv>... action: <name> [params: <kv>...] [priority: <n>]`

插入一条表项。`match:`、`action:`、`params:`、`priority:` 是段
标记，每段内的 key=value 对可以用逗号分隔，逗号两侧空格皆可。
完整示例：

```
p4net> s1 table add MyIngress.ipv4_lpm \
         match: hdr.ipv4.dstAddr=10.0.0.5/32 \
         action: MyIngress.set_egress_port \
         params: port=2
ok
```

ternary 或 range 类型的表需要显式提供 `priority:`；不含
ternary/range 字段的表会拒绝 `priority:`。

### `<switch> table del <table> match: <kv>...`

按匹配键删除一条表项。`match:` 语法与 `table add` 一致。

### `<switch> table clear <table>`

删除指定表的所有表项；返回删除条数。

### `<switch> counter [reset] <name> [<index>]`

读取或清零计数器条目。不带 `reset` 时打印 `pkts=N bytes=M`
（按指定索引，或全部已有条目）。带 `reset` 时把条目清零。

```
p4net> s1 counter MyIngress.ingress_pkts 0
pkts=2 bytes=196
p4net> s1 counter reset MyIngress.ingress_pkts 0
ok
```

### `<switch> mcast list`

按 `<id>: [<port>, ...]` 形式列出所有组播组。

### `<switch> mcast add <id> <port,port,...>`

创建带指定复制端口的组播组。

```
p4net> s1 mcast add 1 1,2,3
ok
```

### `<switch> mcast del <id>`

删除组播组。

### `<switch> packet send <hex_payload> [metadata: <k>=<v>[,<k>=<v>...]]`

通过 StreamChannel 发送一个 `PacketOut`。`<hex_payload>` 是偶数
长度的十六进制字符串，由 `bytes.fromhex` 解析。可选的 `metadata:`
段按名称指定 P4Info 中定义的 `packet_out` 控制器字段；未给出的
字段会被自动补零。

```
p4net> s1 packet send ffffffffffff000000000001880b48656c6c6f \
         metadata: egress_port=1
ok
```

### `<switch> packet listen [count=N] [timeout=T]`

阻塞直至收到 `count` 个数据包或 `timeout` 秒后超时，先到先返回。
默认 `count=1`、`timeout=10.0`。每个数据包渲染一行：
`[ingress_port=N] <hex>...`，hex 部分截断到 64 字符。

```
p4net> s1 packet listen count=2 timeout=3
[ingress_port=1] ffffffffffff000000000001...
[ingress_port=1] 3333000000160000000000018611...
```

## 键盘快捷键

- `Ctrl-C` —— 取消当前输入。若有命令正在执行，对应进程组会
  收到信号，Shell 返回提示符。
- 空行上的 `Ctrl-D` —— 干净退出（与 `exit` / `quit` 等价）。
- `↑` / `↓` —— 历史浏览（持久化于 `~/.p4net_history`）。
- `Tab` —— 顶级命令、主机名、交换机名以及子动词的补全。
