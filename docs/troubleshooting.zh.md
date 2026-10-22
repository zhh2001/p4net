---
description: 通过日志、主机状态和交换机配置，排查启动、P4Runtime 与网络连通性问题。
---

# 故障排查

下文以 `topology.py`、主机 `h1`、`h2` 和交换机 `s1` 为例，操作时替换为实际名称。

## 收集日志和运行状态

激活安装了 p4net 的 Python 环境，然后启用 Python 调试日志、BMv2 日志和抓包：

```bash
sudo env "PATH=$PATH" "$(command -v p4net)" -vv \
  --log-dir ./logs --pcap-dir ./pcaps topology.py
```

进入命令行后，先查看网络和交换机状态：

```text
p4net> status
p4net> switches
p4net> s1 log
```

`switches` 会显示各交换机的 gRPC 地址、PID 和日志路径。`s1 log` 只输出路径，查看内容需在另一个终端执行：

```bash
tail -n 80 ./logs/s1.log
```

如果启动失败、尚未进入命令行，先查看异常说明及其中的日志路径。编译错误会附带 p4c 的标准错误输出，BMv2 启动后才会生成交换机日志。

## 创建命名空间或链路时权限不足

创建命名空间或链路时出现 `Operation not permitted` 或 `Permission denied`，使用上面的 `sudo` 命令启动拓扑。安装 p4net、编译 P4 程序可以使用普通用户。

`sysctl` 写入失败会报告为 `NamespaceError`。根据异常中的键名和命令错误，检查权限、接口是否存在，以及 `sysctl` 命令是否可用。

## 找不到外部命令

`CompilerNotFoundError` 表示找不到 p4c，或 p4c 无法正常运行。`BMv2NotFoundError` 表示找不到 `simple_switch_grpc`。主机命令和命名空间配置还需要 Linux 网络工具：

```bash
command -v p4c simple_switch_grpc ip tc ping sysctl
```

缺少的工具按[安装指南](getting-started/installation.md#required-external-tools)补齐。如果工具安装在自定义目录中，使用 `sudo` 时需像启动命令那样显式传入 `PATH`。寄存器操作还需要 `simple_switch_CLI` 和 BMv2 的 Thrift 服务。

## BMv2 提前退出或 gRPC 端口未就绪

`BMv2StartupError` 会区分进程提前退出（`exited before becoming ready`）和端口等待超时（`did not open gRPC port within 10.0s`）。先查看异常指出的交换机日志，检查 BMv2 参数、接口错误和端口冲突。

BMv2 同时使用 gRPC 和 Thrift 端口。第一个交换机默认使用 50051 和 9090，后续交换机的端口依次加一。分别检查两个监听端口：

```bash
sudo ss -ltnp 'sport = :50051'
sudo ss -ltnp 'sport = :9090'
```

将监听进程的 PID 与 `switches` 中的交换机 PID 对应起来。gRPC 绑定失败后，BMv2 仍可能继续运行 Thrift 服务，进程存活时也需检查日志中的 `Address already in use`。

根据 PID 确认端口所属的实验，关闭对应实验，或选择其他端口。新拓扑可在 `add_switch()` 中通过 `grpc_port` 和 `thrift_port` 指定端口。p4net 按交换机添加顺序分配默认端口，不会探测其他进程是否已占用。

如果交换机确实需要更长的启动时间，直接使用底层 `BMv2Switch` 时可设置 `startup_timeout`，或调用 `wait_until_ready(timeout=...)`。`Network` 使用默认的 10 秒等待时间。

## P4Runtime 连接或写入失败

TCP 端口可连接只是启动检查的第一步。P4Runtime 客户端还需使用正确的 `device_id` 和角色完成仲裁。遇到 `ConnectionError` 或 gRPC `UNAVAILABLE`，先核对 `switches` 中的地址、BMv2 进程及其日志，排除原因后再连接。

p4net 通过 `p4net.control.exceptions` 中的异常报告控制操作失败：

| 异常 | 排查方向 |
| --- | --- |
| `NotPrimaryError` | 控制器选举 ID、角色权限，以及其他已连接的控制器 |
| `PipelineError` | BMv2 JSON 与 P4Info 是否来自同一次编译，以及交换机返回的拒绝原因 |
| `DuplicateEntryError` | 插入前检查该表项是否已经存在 |
| `EntryNotFoundError` | 修改或删除前检查该表项是否存在 |
| `P4RuntimeError` | 查看错误说明和当前操作所需的前置条件 |

读写遇到 `FAILED_PRECONDITION` 时，检查交换机是否已加载转发流水线。独立使用同步客户端时，通过 `set_pipeline_config()` 加载；异步客户端使用 `await push_pipeline()`。`Network.start()` 会为其管理的客户端完成这一步。

P4Runtime 可能用外层 gRPC `UNKNOWN` 包装逐条操作的错误。同步和异步客户端都会按请求顺序，以首个可识别的失败码确定异常类型，无法解析时保留为通用 `P4RuntimeError`。先输出异常类型和说明；需要原始 RPC 信息时，检查 `exc.__cause__`，若其为 `grpc.RpcError`，可调用 `code()`、`details()` 和 `trailing_metadata()`。

异步连接和取消行为见[异步客户端指南](async-client.md)。

## `pingall` 出现失败

依次检查主机地址、路由、邻居解析和 P4 转发配置：

```text
p4net> h1 ifconfig
p4net> h1 cmd ip route
p4net> h1 cmd ip neigh show
p4net> s1 table list
```

`h1 ifconfig` 实际在主机命名空间中运行 `ip -br addr`。邻居项处于 `INCOMPLETE` 或 `FAILED` 状态，表示地址解析尚未成功。列出交换机的表名后，可通过 `s1 table dump` 加表名查看相应表项。

ARP 的处理方式取决于 P4 程序。[快速上手](getting-started/quickstart.md)中的程序在两个端口之间转发以太网帧，包含 ARP，主机可以动态学习邻居。只转发 IPv4 的程序则可能需要静态邻居，或由控制器处理 ARP。

对于快速上手使用的同网段地址和 MAC，可为两端分别配置静态邻居：

```text
p4net> h1 cmd ip neigh replace 10.0.0.2 lladdr 00:00:00:00:00:02 dev h1-eth0 nud permanent
p4net> h2 cmd ip neigh replace 10.0.0.1 lladdr 00:00:00:00:00:01 dev h2-eth0 nud permanent
p4net> pingall
```

静态邻居提供 IP 到 MAC 的映射，转发表项仍需按 P4 程序配置。

## IPv6 地址或连通性不符合预期

先核对拓扑中的 `ip6`、`ip6_a` 和 `ip6_b` 配置，再查看主机接口：

```text
p4net> h1 cmd ip -6 addr show dev h1-eth0
p4net> h1 cmd sysctl net.ipv6.conf.h1-eth0.disable_ipv6
```

p4net 会为配置了 IPv6 地址的主机接口启用 IPv6，并关闭路由器通告处理和 SLAAC。未配置 IPv6 的接口会禁用 IPv6。IPv6 连通还需邻居解析和 IPv6 转发。可以在 P4 程序中转发邻居发现报文、交给控制器处理，或配置静态 IPv6 邻居，具体可参考[双栈示例](examples/dual-stack.md)。

## 命名空间或接口已经存在

启动使用相同节点名称的新拓扑前，先关闭原来的实验。Python 中调用 `net.stop()`，异步代码中使用 `await net.astop()` 等待清理完成。进程被 `SIGKILL` 终止时，无法执行 Python 退出清理。

查看仍存在的资源：

```bash
sudo ip netns list
sudo ip -br link
sudo ip netns exec h1 ip -br link
```

确认 `h1` 和根命名空间中的接口 `s1-eth1` 属于已停止实验的残留后，先删除交换机侧的 veth 接口，再删除命名空间：

```bash
sudo ip link delete s1-eth1
sudo ip netns delete h1
```

主机侧的 `h1-eth0` 通常位于 `h1` 内。删除 veth 任意一端会同时删除另一端。

## 修改 P4 后运行结果没有变化

核对 `add_switch()` 中的 `p4_src` 路径，修改程序后重新启动网络。编译器会在复用缓存前检查预处理结果，包括引用的头文件。已经运行的交换机会继续使用原流水线，直到重新下发或重启。

编译缓存目录需要可写。直接使用编译器时，可以指定当前目录下的缓存并强制编译：

```python
from pathlib import Path

from p4net.compiler import P4Compiler

compiler = P4Compiler(cache_dir=Path(".cache/p4net"))
result = compiler.compile(Path("port_swap.p4"), force=True)
print(result.bmv2_json)
print(result.p4info)
```

## `xterm` 无法打开窗口

主机的 `xterm` 命令需要已安装的 `xterm`、`DISPLAY` 环境变量，以及从该网络命名空间访问 X 服务的能力。先检查启动 p4net 时使用的环境：

```bash
command -v xterm
printenv DISPLAY
```

如果 `sudo` 清除了图形会话环境，启动时需保留 `DISPLAY` 和对应的 X 授权设置，X 服务也需允许该进程访问。只需执行命令时，可以使用命令行中的 `h1 cmd`，例如 `h1 cmd ip -br addr`。

## Graphviz 渲染时找不到 `dot`

渲染 SVG、PNG 或 PDF 需要 Graphviz，可通过 `sudo apt install graphviz` 安装。仅导出 DOT 文件不需要 `dot`：

```text
p4net> topology graph ./topology.dot format=dot
p4net> topology graph ./topology.svg format=svg
```

第二条命令需要 Graphviz。`topology graph` 不带路径时，会直接在命令行输出 DOT 源码。

## 提交问题

[提交 issue](https://github.com/zhh2001/p4net/issues) 时，附上下列版本输出、完整异常、最小拓扑和 P4 程序，以及交换机日志中相关内容：

```bash
python -c "import p4net; print(p4net.__version__)"
p4c --version
simple_switch_grpc --version
```
