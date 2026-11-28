---
description: P4Net 的功能提议，包括 PSA 支持、运行中修改拓扑和异步网络管理。
---

# 路线图

以下提议尚未确定发布版本，可以在 [GitHub Issues](https://github.com/zhh2001/p4net/issues) 讨论。已发布的功能与修复见[变更日志](changelog.md)。

## PSA 架构支持

运行使用可移植交换机架构（PSA）的 P4 程序。P4Net 目前支持 `v1model`，详见 [P4 架构说明](known-limitations.md#3-不支持-psa-架构)。

## 运行时拓扑变更

在网络运行时增删节点和链路。现有版本的操作步骤见[拓扑变更说明](known-limitations.md#4-不支持运行中拓扑变更)。

## 异步网络管理

通过 `async with` 管理网络，或在同一异步应用中协调多个网络的启停。现有接口见[异步网络启停指南](async-client.md#网络启停)。
