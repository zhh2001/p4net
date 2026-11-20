---
description: p4net 的候选扩展，包括 PSA 支持、运行中修改拓扑和异步网络编排。
---

# 路线图

可以在 [GitHub Issues](https://github.com/zhh2001/p4net/issues) 讨论提议。已发布的功能与修复见[变更日志](changelog.md)。

## 候选扩展

这些提议仍需设计，尚未确定发布版本。公开接口的变更遵循 [API 稳定性约定](api-stability.md)。

### PSA 架构支持

p4net 目前通过 `simple_switch_grpc` 运行 `v1model` 程序。支持 PSA 需要集成匹配的编译器和交换机目标，并处理进程启动、就绪检查和流水线安装。

集成后还要确认 p4net 能正确处理新目标生成的 P4Info，并测试表项、计数器、寄存器和数据包收发。

### 运行时拓扑变更

在网络运行时增删节点或链路，需要同时更新拓扑描述、运行中的节点、Linux 命名空间和接口、交换机端口及控制面状态。变更失败时需要回滚，成功创建的资源也要在网络关闭时回收。

现有流程是停止网络、修改拓扑，然后创建并启动新的 `Network`。详见[拓扑变更限制](known-limitations.md#4-不支持运行中拓扑变更)。

### AsyncNetwork

可以基于 `Network.astart()` 和 `Network.astop()` 设计 `AsyncNetwork`，例如提供异步上下文管理器，或协调多个网络的启停。现有方法见[异步生命周期指南](async-client.md#关闭网络)。

## 长期搁置

- **分布式实验**需要远程资源管理、跨机器链路，以及统一协调启动和清理。
- 通过 **Web 界面**在浏览器中查看和控制拓扑。
