---
description: p4net 已发布的版本里程碑，以及面向 2.0 的候选特性清单。
---

# 路线图

## 关于本文档

这是一份滚动路线图，不构成承诺。同一版本内的条目按大致优先级
排列，但相对顺序会随着 issue 与权衡的清晰化而调整。任何条目
都欢迎 PR——请先在对应 issue 中讨论一下，让设计在公开场合先
完成一次，再开始编码。

## 已发布的里程碑

- **v0.1.0**（2026-05-10）—— 首个公开发布。拓扑 DSL、P4Runtime
  控制平面、BMv2 编排、交互式 CLI。详见[变更日志](changelog.md)。
- **v0.2.0**（2026-05-10）—— 控制器数据包 I/O、IPv6 主机寻址、
  非对称链路损伤、拓扑可视化、`xterm` 帮助方法。详见
  [变更日志](changelog.md)。
- **v1.0.0**（2026-05-10）—— 公共 API 稳定承诺、OQ 积压清理
  （phase-13 #1/#2/#4、phase-16 #1）、CI 加固（Ubuntu 22.04 +
  24.04 矩阵、wheel-install smoke 任务）、扩展的 SECURITY.md、
  性能基线、Unicode-aware 文档锚点。详见
  [API 稳定性](api-stability.md)、[性能特征](performance.md)、
  [变更日志](changelog.md)。
- **v1.1.0**（2026-05-11）—— 打磨小版本：INT 示例、统一的
  `p4net.*` logger 层次与 CLI 详细度控制、按方向叠加的链路损伤、
  已知限制清单、自定义 404 页。详见[日志](logging.md)、
  [已知限制](known-limitations.md)、[变更日志](changelog.md)。
- **v1.2.0**（2026-05-11）—— 寄存器 API
  （`P4RuntimeClient.read_register` / `write_register`）、INT 示例的
  `switch_id` 从常量改为寄存器、INT shim 时间戳字段正名为
  `ingress_timestamp_us`、非对称延迟测试容差放宽以消除测试套压力下的
  抖动。详见 [API 稳定性](api-stability.md) 与
  [变更日志](changelog.md)。
- **v1.3.0**（2026-05-11）—— 多跳 INT 示例：两台线性串联的交换机
  各自插入自己的元数据 shim；接收端解析整条逐跳栈。同一份 P4 二进制
  在两台交换机上运行；逐交换机身份通过 v1.2 寄存器 API 提供。无公共
  API 变化。详见 [变更日志](changelog.md)。
- **v1.4.0**（2026-05-11）—— 通过 `RunningSwitch.boot_timestamp_us`
  （BMv2 进程启动时的 Unix 微秒挂钟值）实现跨交换机 INT 时间戳对齐。
  多跳 INT 示例在启动时写入协调文件；listener 显示对齐后的挂钟
  时间戳和逐跳转发延迟。详见 [变更日志](changelog.md)。
- **v1.5.0**（2026-05-11）—— 新增 `Network.boot_timestamps` 便利封装
  （一次性返回全部交换机的启动时间戳字典）；多跳 INT 示例支持通过
  环境变量 `P4NET_INT_BOOT_TIMES_PATH` 指定协调文件路径，从而在同一
  主机上并行运行多份拓扑不互相覆盖。详见 [变更日志](changelog.md)。
- **v1.5.1**（2026-05-12）—— 撤掉多跳 INT 集成测试里设计有误的
  aligned-causal 断言。详见 [变更日志](changelog.md)。

## 1.x 候选项（仅追加，不破坏 API）

目前没有公开的候选项；欢迎通过 issue 提议。

## 2.0 候选项（可能需要破坏 API）

### PSA 架构支持

超出 v1model。需要 `p4c` 与 BMv2 带 PSA 目标编译（多数发行版的
标准包不带）。编译器封装层需要学会按架构选择对应的
`simple_switch` 变体；`P4InfoIndex` 不需要变化，因为 P4Runtime
把架构与控制器解耦。放到 2.0 是因为 `Topology.add_switch` 签名
可能需要新增必填参数或重新设计默认值。

### 运行时拓扑变更

`Network.add_host(...)`、`Network.add_link(...)` 以及对应的
`remove_*`，在网络已经运行时使用。目前 `Topology` 在 `start()`
时即冻结。底层原语（命名空间、veth 对）已经支持在线增删；编排
器的簿记只需要复用同一套代码路径。放到 2.0 是因为 `Topology`
的不可变约定在 1.x 中作为稳定 API 的一部分被记录。

### 异步 P4Runtime 客户端

基于 `aio-grpc` 的 `P4RuntimeClient` 变体，面向事件驱动的控制器
（例如同时订阅多台交换机 StreamChannel 的场景）。可能与现有的
线程版本并存，共用同一个 `P4InfoIndex`，而不是替换它。如果新
客户端能独立成 `AsyncP4RuntimeClient` 而不破坏现有客户端，
也可以在 1.x 提供。

## 长期搁置

- **跨多机的分布式仿真**。把两套 p4net 跨网络联起来需要主机间
  L2 隧道与一层协调；当前架构对此没有任何设计。
- **拓扑可视化与运行时状态的 Web UI**。如果它真的发生，会以
  独立仓库的形式存在。保持 p4net 本身不依赖前端框架。
