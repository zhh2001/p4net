---
description: p4net 常见故障——缺工具、sysctl 错误、BMv2 启动卡住、gRPC 异常、残留 veth、IPv6 噪声等——的现象、原因与修复方法。
---

# 故障排查

下列每个条目都是 p4net 开发或使用过程中真实遇到过的故障模式。
顺序：现象、原因、修复。

## "Permission denied" 创建命名空间或 veth

!!! warning "现象"
    `ip netns add`、`ip link add ... type veth` 或 `tc qdisc add`
    报 `PermissionError: [Errno 13] Permission denied`。

**原因。** p4net 需要 `CAP_NET_ADMIN` 来操作命名空间、链路、qdisc。
无 root 也无该 capability 时，所有触网络的调用都会失败。

!!! tip "修复"
    用 `sudo` 运行。如果 `sudo` 把虚拟环境从 `PATH` 中剥离了，
    使用 `sudo env "PATH=$PATH" p4net <topology.py>` 或者绝对
    路径：`sudo "$(. .venv/bin/activate && which p4net)" ...`。

    对 Python 解释器执行 `setcap cap_net_admin+ep` 原理上可行，
    但与 shebang 查找、setuid 等机制有不良交互；除非你清楚利弊，
    不推荐这条路。

## "p4c not found" / "simple_switch_grpc not found"

!!! warning "现象"
    `FileNotFoundError: [Errno 2] No such file or directory:
    'p4c'` 或 `'simple_switch_grpc'`。

**原因。** 外部编译器或 BMv2 二进制不在 `PATH` 上。

!!! tip "修复"
    用 `which p4c` 与 `which simple_switch_grpc` 验证。已安装但
    不在 `PATH` 上时，调整 Shell 的 `PATH`，或显式给 p4net 传
    `PATH`（`sudo env "PATH=$PATH" ...`）。未安装时见
    [安装](getting-started/installation.md)中的「必需的外部工具」一节。

## BMv2 启动卡住

!!! warning "现象"
    `Network.start()` 在 "waiting for BMv2 gRPC to become ready"
    处停留，最终超时（默认 10 秒）。

**原因。** BMv2 启动并绑定 gRPC 端口耗时超过 10 秒。常见原因：
主机负载高、内核换页、紧密循环里反复重启 `simple_switch_grpc`
而上一个实例尚未释放端口。

!!! tip "修复"
    给 `Network` 传更长的就绪超时：

    ```python
    Network(topo, bmv2_grpc_ready_timeout=30.0)
    ```

    若仍卡住，看 `<log_dir>/<switch>.log` 中的 BMv2 日志——
    编排器会把它落盘。常见的关键行是 `Could not bind: Address
    already in use`，意味着旧实例没退干净。

## P4Runtime 客户端报 gRPC `UNAVAILABLE`

!!! warning "现象"
    `client.connect()` 或第一次 `set_pipeline_config` 抛
    `grpc.RpcError: <_InactiveRpcError ... StatusCode.UNAVAILABLE
    ...>`。

**原因。** 与 BMv2-启动-卡住 同源：客户端尝试连接尚未绑定端口
的 BMv2。

!!! tip "修复"
    编排器在 `connect()` 之前会轮询 gRPC 端口，所以这个错误在
    托管路径下很少出现。如果你直接驱动 `P4RuntimeClient`，请
    自行加就绪轮询，或接受偶发失败并重试。

## 错误 `UNKNOWN` 携带难以理解的字节负载

!!! warning "现象"
    某次写操作失败，得到 `grpc.RpcError(StatusCode.UNKNOWN, ...)`，
    且消息里夹杂二进制数据。

**原因。** BMv2 把每条更新的错误细节打包进 `grpc-status-details-bin`
trailer（一个序列化的 `google.rpc.Status`，内含若干 `p4.v1.Error`
消息和数字 canonical code）。p4net 的
`P4RuntimeClient._translate_rpc_error` 会把它们解码成具体异常
（`DuplicateEntryError`、`EntryNotFoundError`、`PipelineError`
等），前提是该 trailer 存在。

!!! tip "修复"
    确认你使用的 p4net ≥ 0.1.0（codec 在 phase 5 落地）。如果
    你直接访问了 `client.last_error`，请把异常的 `details()` 与
    `trailing_metadata()` 都打印出来——原始状态在
    `grpc-status-details-bin` 里。

## `pyroute2.NetlinkError(17, 'File exists')` 创建 link 时

!!! warning "现象"
    `Network.start()` 抛 `LinkError: interface 'h1-eth0' already
    exists` 或类似 errno 17 的 netlink 错误。

**原因。** 上一次运行残留了 veth 对。正常情况下 atexit 清理或
SIGINT 处理器会回收它们；但若进程被 `SIGKILL` 干掉（如 `kill -9`
或 OOM），或处理器尚未注册就退出，回收路径不会跑。

!!! tip "修复"
    用 `ip link show` 查找——残留 veth 通常名为 `<host>-eth<N>`。
    删除：

    ```
    sudo ip link del h1-eth0
    sudo ip netns del h1
    ```

    然后重跑。

## 老版本中交换机侧 veth 出现 IPv6 link-local

!!! warning "现象"
    在 p4net < 0.2.0 上，`<host> ifconfig` 与 `<switch> log` 看到
    多余的 `fe80::` 地址；punt 路径抓包出现 MLD 报文。

**原因。** `net.ipv6.conf.<iface>.disable_ipv6=0` 时，Linux 内核
会在每个被拉起的接口上自动生成 `fe80::` link-local 地址。0.2.0
之前没有在拉起前关掉这个 sysctl。

!!! tip "修复"
    升级到 p4net ≥ 0.2.0。编排器现在会先在每个未显式启用 IPv6
    的接口上写 `disable_ipv6=1`，再把接口拉起；显式启用了
    `Host.ip6` 的接口会得到 `disable_ipv6=0, accept_ra=0,
    autoconf=0`。

## pingall 全部失败

!!! warning "现象"
    `pingall` 矩阵全是 `X`；任意主机都 ping 不通其他主机。

**原因。** 最常见的是没注入 ARP。BMv2 没有内置 ARP 响应器；不
注入静态邻居，第一次 ICMP 请求触发的 ARP 广播在数据平面没有
对应规则。

!!! tip "修复"
    示例都在 `setup(net)` 中注入 ARP：

    ```python
    h1.exec(["ip", "neigh", "replace", "10.0.0.2",
             "lladdr", "00:00:00:00:00:02",
             "dev", "h1-eth0", "nud", "permanent"])
    ```

    要么对每对主机都注入，要么在 P4 程序里实现 ARP 响应器
    （比如经由 CPU 端口 punt 路径）。

## 改了 P4 源码但缓存似乎没失效

!!! warning "现象"
    修改 `.p4` 文件后，运行的流水线里看不到改动。

**原因。** 几乎从来不是缓存问题——缓存键是源码字节，但还是值得
排查一下。

!!! tip "修复"
    `rm -rf ~/.cache/p4net/compiler/` 清空缓存；下一次运行会重
    填。如果还有问题，那就不是缓存——确认 `add_switch(p4_src=...)`
    指向了正确的 `.p4` 路径。

## xterm 报 "DISPLAY not set"

!!! warning "现象"
    `<host> xterm` 返回 `error: NetworkError: cannot spawn xterm:
    $DISPLAY is unset`。

**原因。** 编排器进程访问不到 X server。`xterm` 需要
`$DISPLAY` 已设置且 X socket 可达。

!!! tip "修复"
    桌面 Linux 直接可用。SSH 远程则用 `ssh -X user@host`，若服务
    端要求受信转发就用 `-Y`。WSL2 上需要先装 X server（VcXsrv、
    转发到 XQuartz 等）并设置 `DISPLAY`。无头服务器上 `xterm`
    根本不适用——一次性命令请用 `<host> cmd <argv>`。

## BMv2 gRPC 端口报 "Address already in use"

!!! warning "现象"
    BMv2 启动失败，`<log_dir>/<switch>.log` 出现 `Could not bind:
    Address already in use`。

**原因。** 另一个 `simple_switch_grpc` 进程（或其他进程）已经占
用了编排器选择的端口。

!!! tip "修复"
    查找并终止占用方：

    ```
    sudo ss -ltnp | grep <port>
    sudo pkill simple_switch_grpc
    ```

    编排器从 50051 开始分配端口；如果你长期遇到冲突，对各交换机
    显式覆盖：`topology.add_switch("s1", ..., grpc_port=50061)`。

## `topology graph` 报找不到 `dot`

!!! warning "现象"
    `topology graph /tmp/topo.png` 返回 `error: TopologyError:
    graphviz \`dot\` binary not found on PATH`。

**原因。** 未安装 `graphviz`。

!!! tip "修复"
    安装它（`sudo apt install graphviz`），或用 `format=dot` 直接
    输出源码：

    ```
    p4net> topology graph /tmp/topo.dot format=dot
    ```

    然后用任何识别 DOT 的工具（在线渲染器、`gxl2dot` 等）渲染。

## 上面没覆盖你的问题

请到 <https://github.com/zhh2001/p4net/issues> 提交 issue，附上
`python -c "import p4net; print(p4net.__version__)"`、`p4c
--version`、`simple_switch_grpc --version`、拓扑文件（或最小复现
代码），以及 `<log_dir>/<switch>.log` 中相关日志。
