---
description: p4net 公开接口的稳定性等级、弃用规则和各包分类。
---

# API 稳定性

稳定 API 在 p4net 1.x 内保持兼容。下表按各包的 `__all__` 导出项分类。类的等级也适用于文档公开的构造参数、方法和属性，单独列出等级的成员除外。顶层的 `p4net.Network` 与 `p4net.network.Network` 是同一个类。

## 稳定性等级

| 等级 | 兼容规则 |
| --- | --- |
| 稳定（Stable） | 名称、签名和文档规定的行为在 1.x 内保持兼容。不兼容变更需经过弃用周期，并在新的主版本中发布。 |
| 暂定（Provisional） | 经过弃用周期后，可以在次版本中进行不兼容变更。 |
| 实验性（Experimental） | 接口可能不经预告调整，1.x 内也适用。 |
| 内部（Internal） | 不提供公开兼容承诺，包括私有成员和未由包导出的实现细节。 |

修复与文档不符的行为，以及新增带默认值的关键字参数等兼容性扩展，不需要弃用周期。

## 弃用流程

稳定和暂定 API 标为弃用后，在调用时发出 `DeprecationWarning`。接口至少在两个连续次版本中保持可用，包括开始发出警告的版本和下一个次版本。

例如，1.2 中弃用的接口在 1.3 中仍可使用。暂定 API 最早可在 1.4 中移除或进行不兼容变更。稳定 API 也要完成这个周期，但只能在下一个主版本中移除或进行不兼容变更。实验性 API 不要求经过这一周期。

## 异步客户端 { #临时provisional等级与异步客户端 }

`AsyncP4RuntimeClient`、`AsyncOperationCancelledError` 和 `RunningSwitch.async_client` 在 1.6.0 中以暂定 API 引入，1.7.0 起属于稳定 API，遵循相同的兼容规则。

同步和异步客户端的调用方式、部分方法和返回值不同。例如，同步客户端通过 `set_pipeline_config()` 安装流水线，异步客户端使用 `await push_pipeline()`。完整差异和事件循环要求见[异步客户端指南](async-client.md)。

## 各包分类

### `p4net.topo`

| 符号 | 等级 | 范围 |
| --- | --- | --- |
| `Host`、`P4Switch` | 稳定 | 文档公开的数据字段及其类型。可以增加带兼容默认值的字段。 |
| `Link`、`LinkEndpoint` | 稳定 | 端点字段、MTU、对称链路参数、分方向覆盖参数，以及延迟、抖动和丢包的叠加参数。 |
| `Topology` | 稳定 | 节点和链路创建、查询、校验、Graphviz 导出及序列化。`to_dict()` / `from_dict()` 的往返格式也属于兼容范围。 |
| `TopologyError` | 稳定 | 拓扑校验错误。 |

### `p4net.network`

| 符号 | 等级 | 范围 |
| --- | --- | --- |
| `Network` | 稳定 | 启停、主机和交换机访问、`ping()`、`pingall()`、`pingall6()`。在事件循环中，使用 `await net.astart()` 和 `await net.astop()` 等待启动和关闭。取消时先完成清理。 |
| `Network.xterm` | 实验性 | 通过可用的 X 显示服务打开主机终端。 |
| `Network.boot_timestamps` | 稳定 | 每次返回一个新字典，将交换机名映射到 BMv2 启动时的 Unix 微秒时间戳。修改该字典不影响网络状态。网络未运行时抛出 `NetworkNotRunningError`。 |
| `RunningHost` | 稳定 | 主机描述、命名空间和接口访问、地址、命令执行及 ping。 |
| `RunningSwitch` | 稳定 | 交换机描述、BMv2 进程、同步客户端、编译产物及日志路径。 |
| `RunningSwitch.async_client` | 稳定，自 1.7 起 | 首次访问时创建未连接的客户端，并传入同步客户端当前的 P4Info 索引。实例会缓存，直到 `stop()` 或 `astop()` 将其关闭并清除。 |
| `RunningSwitch.boot_timestamp_us` | 稳定 | BMv2 启动时的 Unix 微秒时间戳。底层进程没有启动时间戳时抛出 `NetworkNotRunningError`。 |
| `NetworkError`、`NetworkAlreadyRunningError`、`NetworkNotRunningError`、`NodeNotFoundError` | 稳定 | 网络启停和节点查询错误。 |

### `p4net.control`

| 符号 | 等级 | 范围 |
| --- | --- | --- |
| `P4RuntimeClient` | 稳定 | 同步连接管理、流水线配置、表项、计数器、寄存器、组播组和报文收发。 |
| `AsyncP4RuntimeClient` | 稳定，自 1.7 起 | 异步连接管理、流水线安装、表项、计数器、寄存器和报文收发。与同步接口的差异见[异步指南](async-client.md#与同步接口的区别)。 |
| `AsyncOperationCancelledError` | 稳定，自 1.7 起 | 异步客户端的取消异常，继承 `P4RuntimeError`。具体触发条件见[异步指南](async-client.md#错误超时与取消)。 |
| `P4RuntimeClient.read_counter`、`P4RuntimeClient.reset_counter` | 稳定 | 读取计数器单元，或将其报文数和字节数清零。 |
| `P4RuntimeClient.read_register`、`P4RuntimeClient.write_register`、`AsyncP4RuntimeClient.read_register`、`AsyncP4RuntimeClient.write_register` | 稳定 | 通过 BMv2 的 Thrift 服务和 `simple_switch_CLI` 读取寄存器单元或数组、写入单个单元。 |
| 自动生成选举 ID 的策略 | 暂定 | `Network` 和未显式指定 ID 的异步客户端使用毫秒时间戳。直接创建的同步客户端默认使用 `(1, 0)`。显式传入的 `(high, low)` 参数格式保持稳定。 |
| `P4InfoIndex` | 稳定 | P4Info 加载、表/动作/计数器/寄存器查询、匹配和动作编码，以及控制器元数据处理。下方单列的格式化方法有自己的等级。 |
| `P4InfoIndex.decode_match` | 暂定 | 按 P4Info 字段位宽格式化匹配值：32 位为 IPv4，48 位为 MAC，128 位为 IPv6，其他位宽为十进制。 |
| `CounterData` | 稳定 | `packet_count` 和 `byte_count`。 |
| `RegisterSpec` | 稳定 | 不可变的寄存器元数据：`id`、`name`、`bitwidth`、`size`。 |
| `encode_value`、`encode_int`、`encode_ipv4`、`encode_mac`、`decode_int`、`decode_ipv4`、`decode_ipv6`、`decode_mac` | 稳定 | 值的编码和解码。 |
| `parse_lpm`、`parse_ternary`、`parse_range`、`format_exact`、`format_lpm`、`format_ternary`、`format_range`、`canonicalize` | 稳定 | 匹配值的解析、显示格式和规范字节表示。 |
| `P4RuntimeError`、`ConnectionError`、`EncodingError`、`DuplicateEntryError`、`EntryNotFoundError`、`NoSuchTableError`、`NoSuchActionError`、`NoSuchFieldError`、`NoSuchRegisterError`、`NotPrimaryError`、`PipelineError` | 稳定 | 控制面错误。 |

### `p4net.runtime`

这些底层接口用于直接管理 Linux 资源和 BMv2 进程，`Network` 将它们组合为网络的启动和清理流程。

| 符号 | 等级 | 范围 |
| --- | --- | --- |
| `NetworkNamespace`、`VethPair`、`NSProcess` | 暂定 | 命名空间、接口及进程管理。 |
| `BMv2Switch` | 暂定 | BMv2 启动、监听端口归属检查、进程状态和关闭。 |
| `apply_netem`、`clear_qdisc`、`disable_ipv6`、`enable_ipv6` | 暂定 | 流量控制和接口的 IPv6 配置。 |
| `P4NetError`、`PrivilegeError`、`NamespaceError`、`LinkError`、`TcError`、`BMv2NotFoundError`、`BMv2StartupError` | 稳定 | 运行时错误，以及公共异常基类 `P4NetError`。 |

### `p4net.compiler`

| 符号 | 等级 | 范围 |
| --- | --- | --- |
| `P4Compiler` | 暂定 | 编译器配置和缓存管理。编译方法遵循下方的稳定约定。 |
| `P4Compiler.compile` | 稳定 | 接收 P4 源码路径、架构、强制编译和额外参数，返回 `CompileResult`。缓存目录布局属于实现细节。 |
| `CompileResult` | 稳定 | 编译结果信息，包括 `bmv2_json` 和 `p4info` 路径。 |
| `CompileError`、`CompilerNotFoundError` | 稳定 | 编译器拒绝源码，以及编译器缺失或版本检查失败的错误。均继承 `P4NetError`。 |

### `p4net.cli`

| 接口 | 等级 | 范围 |
| --- | --- | --- |
| 交互命令 | 稳定 | [CLI 参考](cli.md)中的命令语法和行为。 |
| `p4net` 控制台入口 | 稳定 | 拓扑文件参数、`--no-shell`、`--unsafe`、`--log-dir`、`--pcap-dir`、`--extra-compile-arg` 和日志详细程度选项。 |
| `CommandDispatcher`、`P4NetShell` | 实验性 | 实现命令分发和交互终端的 Python 类。 |
| `CLIError`、`CLIExit`、`CLIUsageError` | 稳定 | CLI 错误和退出信号。 |

## 支持范围

p4net 在单台 Linux 机器上使用 BMv2 运行 v1model 程序，同时支持同步和异步 P4Runtime 客户端。目前不支持 PSA 目标、运行中的拓扑变更和跨机器分布式编排。具体限制见[已知限制](known-limitations.md)，后续方向见[路线图](roadmap.md)。

## 日志命名空间

`p4net` 及其 `topo`、`network`、`control`、`runtime`、`compiler`、`cli` 日志子树保持稳定。可通过标准 Python 日志接口配置，例如 `logging.getLogger("p4net.runtime")`。

具体日志文本和某个事件的日志级别可能调整，配置方法见[日志指南](logging.md)。

## 反馈兼容性问题

稳定 API 的兼容性回归按缺陷处理。请在[问题跟踪](https://github.com/zhh2001/p4net/issues)中提供受影响的版本和最小复现。
