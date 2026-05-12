---
description: 按公开符号划分的 API 稳定性等级——1.0 含义、弃用流程、各包等级表。
---

# API 稳定性

p4net 的 1.x 系列承诺一份稳定的公共 API。本页定义「稳定」的实际
含义、按等级分类每一个公开符号，并给出弃用流程。

## 「1.0」对 p4net 意味着什么

1.x 不会在没有弃用流程的情况下破坏**稳定**等级的 API。一些 API 被
显式标记为**临时**——它们在 1.x 内可能经过弃用流程后演进。一些
API 被显式标记为**实验性**——它们在 1.x 内可能在没有任何提示的
情况下变化，请勿在难以重构的代码中依赖它们。任何未在 `__all__`
中导出的对象都视为**内部**对象，无论名称是否带下划线，都不要
直接 import。

## 稳定性等级

**稳定**。在整个 1.x 内向后兼容。行为变更仅在 2.0+。修复 bug 同时
不破坏文档化契约的改动随时允许。

**临时**。可能在 1.x 的某个次版本中变化，但前提是在至少两个连续
次版本中已经显示弃用警告。新增的「带默认值的关键字参数」不算
破坏性变化，可以无弃用周期添加。

**实验性**。在 1.x 内可能随时变化，无须警告。请勿依赖精确接口；
如需可重现性，请把版本号锁死。

**内部**。不论语言层面是否可访问，都不属于公共 API。请勿直接
import。任何未在包的 `__all__` 中导出的对象都属于此类。

## 弃用流程

- 弃用的 API 在调用点抛出 `DeprecationWarning`。
- 弃用的 API 至少在两个连续次版本中保留可用（例如 1.2 弃用 →
  1.3 仍可用 → 最早 2.0 移除；或者对**临时**与**实验性** API，
  在弃用周期结束后早于 2.0 移除）。
- 在次版本中移除仅允许针对临时与实验性 API，且需走完弃用周期。
- 稳定 API 只能在主版本（2.0+）中移除，且需要在前一个主版本里
  完整经历一个次版本周期的弃用。

## 各包分类

### `p4net.topo`

用户面向的拓扑 DSL。整体稳定。

| 符号 | 等级 | 备注 |
| --- | --- | --- |
| `Host` | 稳定 | 字段名与类型稳定。可在带安全默认值的前提下追加新字段。 |
| `P4Switch` | 稳定 | 同 `Host`。 |
| `Link` | 稳定 | 对称与非对称损伤字段（`bandwidth`、`delay`、`jitter`、`loss_pct` 及对应的 `*_a_to_b` / `*_b_to_a`）均稳定。1.1 引入的每方向叠加字段（`delay_a_to_b_extra`、`delay_b_to_a_extra`、`jitter_a_to_b_extra`、`jitter_b_to_a_extra`、`loss_pct_a_to_b_extra`、`loss_pct_b_to_a_extra`）同为稳定。 |
| `LinkEndpoint` | 稳定 | |
| `Topology` | 稳定 | `add_host`、`add_switch`、`add_link`、`validate`、`to_graphviz`、`render_graphviz`、`to_dict`、`from_dict` 均稳定。`to_dict` / `from_dict` 的往返格式属于公共契约。 |
| `TopologyError` | 稳定 | |

### `p4net.network`

用户面向的部分稳定；底层细节临时。

| 符号 | 等级 | 备注 |
| --- | --- | --- |
| `Network` | 稳定 | `start`、`stop`、`host`、`switch`、`pingall`、`pingall6`、`boot_timestamps`、`__enter__`、`__exit__` 均稳定。 |
| `Network.boot_timestamps` | 稳定 | 只读字典：交换机名 → 该交换机 BMv2 进程启动时的 Unix 微秒挂钟值。是 `RunningSwitch.boot_timestamp_us` 的便利封装；每次调用返回新字典。在 `start` 之前或 `stop` 之后访问会抛出 `NetworkNotRunningError`。 |
| `Network.xterm` | 实验性 | 依赖 `$DISPLAY`；随着派生进程跟踪机制完善，行为可能演进。 |
| `RunningHost` | 稳定 | `name`、`primary_ip`、`primary_ip6`、`interfaces`、`interfaces6`、`ping`、`exec`、`popen` 均稳定。 |
| `RunningSwitch` | 稳定 | `name`、`client`、`bmv2`、`compile_result`、`log_file`、`descriptor`、`boot_timestamp_us` 均稳定。 |
| `RunningSwitch.boot_timestamp_us` | 稳定 | 该交换机 BMv2 进程启动时的 Unix 时间戳（微秒）。配合 INT shim 的 `ingress_timestamp_us` 计算挂钟到达时间，对齐跨交换机的时间戳。在 `start` 之前或 `stop` 之后访问会抛出 `NetworkNotRunningError`。 |
| `NetworkError` 及子类 | 稳定 | `NetworkAlreadyRunningError`、`NetworkNotRunningError`、`NodeNotFoundError`。 |

### `p4net.control`

客户端 API 稳定；codec 边缘情况临时。

| 符号 | 等级 | 备注 |
| --- | --- | --- |
| `P4RuntimeClient` | 稳定 | `connect`、`disconnect`、`set_pipeline_config`、`get_pipeline_config`、`insert_table_entry`、`modify_table_entry`、`delete_table_entry`、`list_table_entries`、`clear_table`、`read_counter`、`write_counter`、`add_multicast_group`、`modify_multicast_group`、`delete_multicast_group`、`list_multicast_groups`、`send_packet_out`、`on_packet_in`、`expect_packet_in`、`read_register`、`write_register`。 |
| `P4RuntimeClient.read_register` / `write_register` | 稳定 | 读取单个 cell 或整个数组；写入单个 cell。BMv2 的 PI 不实现 P4Runtime RegisterEntry，因此实现走 BMv2 Thrift 控制通道（`simple_switch_CLI`）；Python API 契约与符合 P4Runtime 标准的目标一致。 |
| `P4InfoIndex.register_by_name` | 稳定 | 按名查找返回 `RegisterSpec`；不存在抛出 `NoSuchRegisterError`。 |
| `RegisterSpec` | 稳定 | 不可变 dataclass，含 `id`、`name`、`bitwidth`、`size`。 |
| `NoSuchRegisterError` | 稳定 | 寄存器名不存在时由查找接口抛出。 |
| `P4RuntimeClient` 选举 ID | 临时 | 「毫秒时间戳」生成器与外部类型（`tuple[int, int]`）稳定；如果带来运维压力，策略可能变化。 |
| `P4InfoIndex` | 稳定 | 顶层查询（`tables`、`actions`、`counters`、控制器报头元数据）稳定。 |
| `P4InfoIndex.decode_match` | 临时 | 位宽感知格式化规则（32 位 → IPv4，48 位 → MAC，128 位 → IPv6，其余十进制）在 1.x 内稳定；如需为新字段类型添加规则，会走弃用流程。 |
| `CounterData` | 稳定 | |
| Codec 帮助函数（`encode_value`、`encode_int`、`encode_ipv4`、`encode_mac`、`decode_int`、`decode_ipv4`、`decode_ipv6`、`decode_mac`、`parse_lpm`、`parse_ternary`、`parse_range`、`format_exact`、`format_lpm`、`format_ternary`、`format_range`、`canonicalize`） | 稳定 | 它们消费与产出的字符串格式属于公共契约。 |
| 异常类型（`P4RuntimeError`、`ConnectionError`、`EncodingError`、`DuplicateEntryError`、`EntryNotFoundError`、`NoSuchTableError`、`NoSuchActionError`、`NoSuchFieldError`、`NotPrimaryError`、`PipelineError`） | 稳定 | |

### `p4net.runtime`

临时。它们是有意保留的低层逃生口；高层 `Network` 编排器涵盖了
大多数使用场景。

| 符号 | 等级 | 备注 |
| --- | --- | --- |
| `NetworkNamespace` | 临时 | |
| `VethPair` | 临时 | |
| `BMv2Switch` | 临时 | |
| `NSProcess` | 临时 | |
| `apply_netem`、`clear_qdisc` | 临时 | |
| `disable_ipv6`、`enable_ipv6` | 临时 | |
| 异常类型（`LinkError`、`NamespaceError`、`BMv2NotFoundError`、`BMv2StartupError`、`TcError`、`PrivilegeError`、`P4NetError`） | 稳定 | |

### `p4net.compiler`

实现细节临时；编译器接口稳定。

| 符号 | 等级 | 备注 |
| --- | --- | --- |
| `P4Compiler` | 临时 | `compile()` 方法的行为稳定；`~/.cache/p4net/compiler/` 下的缓存目录布局是临时的，请勿直接访问缓存。 |
| `CompileResult` | 稳定 | `bmv2_json` 与 `p4info` 的 Path 属性属于公共契约。 |
| `CompilerError` | 稳定 | |

### `p4net.cli`

CLI 命令语法（用户在提示符里输入的内容）作为面向用户的契约
**稳定**——[CLI 参考](cli.md)中记录的每条命令在 1.x 中行为
一致。实现这些命令的 Python 类结构是**实验性**的；外部代码不
应该继承 `CommandDispatcher` 或 `P4NetShell`。

| 符号 | 等级 | 备注 |
| --- | --- | --- |
| CLI 命令语法 | 稳定 | 记录于 [CLI 参考](cli.md)。 |
| `CommandDispatcher`（Python 类） | 实验性 | 实现细节；不支持继承。 |
| `P4NetShell`（Python 类） | 实验性 | 实现细节；不支持继承。 |
| `CLIError`、`CLIExit`、`CLIUsageError` | 稳定 | |
| `p4net` 控制台脚本（argv 契约） | 稳定 | `--no-shell`、`--unsafe`、`--log-dir`、`--pcap-dir`、`--extra-compile-arg`。 |

## 1.x 显式不支持的功能

- **PSA 架构**。仅支持 v1model。PSA 支持计划在 2.0 提供；见
  [路线图](roadmap.md)。
- **运行时拓扑变更**。构建 `Topology`，交给 `Network`，工作，
  然后 `stop`。在 1.x 不支持运行时增删主机/交换机/链路；计划
  2.0 提供。
- **异步 P4Runtime 客户端**。当前客户端是同步阻塞 gRPC。基于
  asyncio + `grpc.aio` 的变体属于 1.x 之后。
- **跨多机分布式仿真**。仅支持单机。

## Logger 命名空间

`p4net.*` logger 命名空间结构在 1.x 是**稳定**的。通过
`logging.getLogger("p4net")` 或任意已记录的子包 logger
（`p4net.network`、`p4net.runtime`、`p4net.control`、
`p4net.compiler`、`p4net.topo`、`p4net.cli`）访问的代码将持续
可用。可能会新增子模块 logger；已有的不会被改名。

**日志文本**与**某个具体事件被选用的级别**不在稳定契约范围内
——请参见[日志](logging.md)了解级别分类与理由。

## 反馈兼容性问题

我们把稳定 API 上的兼容性回归当作 bug，会在补丁版本中修复。
如果某个 1.x 版本仅使用稳定 API 的代码在另一个 1.x 上不工作，
请到 <https://github.com/zhh2001/p4net/issues> 提交带最小复现的
issue。
