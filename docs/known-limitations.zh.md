---
description: p4net 1.x 不支持哪些能力，以及在有变通方案时的处理方式。
---

# 已知限制

p4net 的 1.x 系列有意识地放下了若干能力——要么需要破坏性变更
（计划放到 2.0+），要么是底层 BMv2 软件数据平面本身的限制。本页
把这些边界一次性列出来，方便用户在自己的代码里撞上之前先看到。

## 1. 每个 Python 进程只支持一个 `Network` 生命周期

- **现象**：在同一个 Python 进程里启动第二个 `Network`，或者反复
  start/stop/start，最终会在某个 `subprocess.run` 或 `Popen` 调用
  上死锁。
- **原因**：gRPC 的 `StreamChannel` 会拉起后台线程。`subprocess.run`
  fork 时，Python 多线程 fork 的固有问题会浮现，子进程可能持有
  陈旧锁卡死。
- **变通方案**：所有交换机交互改用 `AsyncP4RuntimeClient`（而非
  `P4RuntimeClient`）。异步客户端走 `grpc.aio`，不起后台线程，因此
  多线程 fork 的坑不适用。详见[异步客户端](async-client.md)。如果
  同步代码不便迁移，仍可用「每次拓扑跑都开新 Python 进程」的老方法
  ——`subprocess.run([sys.executable, "topology.py"])`；
  `docs/performance.md` 的测量脚本就是这种写法。
- **状态**：v1.6 引入的异步客户端结构性地解决了 per-RPC 操作的
  问题。`Network.start()` 本身仍然是同步的，并用线程并行起 BMv2
  ——未来的 `AsyncNetwork` 会把最后这一段堵上。

## 2. 仅支持单机运行

- **现象**：`Network` 只会在当前机器上拉起主机、交换机和链路。
- **原因**：设计取舍。跨机协同需要分布式控制平面和 L2 隧道，都不
  在 1.x 范围内。
- **变通方案**：p4net 1.x 内部没有。要做多机拓扑，请在每台机器
  各自跑 `Network`，外面用你熟悉的隧道桥接。
- **状态**：2.0 候选。

## 3. 不支持 PSA 架构

- **现象**：基于 `psa.p4` 的 P4 程序在 `P4Compiler` 里编译失败，
  或者编出来的 P4Info 没法被运行时驱动。
- **原因**：PSA 的解析/反解析约定、元数据布局、`P4Info` 模式都
  与 v1model 不同。完整支持需要编译器包装层与 switch 二进制都做
  架构感知。
- **变通方案**：用 v1model。几乎所有的 P4 教程和教学流水线都用
  的是 v1model。
- **状态**：2.0 候选。

## 4. 不支持运行中拓扑变更

- **现象**：在 `network.start()` 之后再调用 `topology.add_host(...)`
  或 `topology.add_link(...)` 不会对运行中的网络产生任何效果。
- **原因**：BMv2 没法干净地动态重配端口，而中途改 veth 对会与正在
  跑的 P4 流水线打架。
- **变通方案**：先 `stop`，改 `Topology`，再启动一个新的 `Network`。
- **状态**：2.0 候选。

## 5. BMv2 吞吐受限

- **现象**：拓扑整体吞吐很少超过 1–10 Mbps；典型每跳延迟 1–10 ms。
- **原因**：BMv2 是面向正确性与可调试性的软件交换机，不是面向
  性能的。它的指令级解释器循环就是瓶颈。
- **变通方案**：没有——这是 BMv2 的设计取向。生产负载请上真正的
  P4 ASIC（Tofino、dpdk-bmv2 变体）。
- **状态**：固有限制；不在 p4net 路线图内。
