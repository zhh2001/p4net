---
description: p4net 的部署范围、架构支持、拓扑变更限制、gRPC 进程约束和性能影响因素。
---

# 已知限制

p4net 使用 Linux 网络命名空间、veth 接口和 BMv2 运行 P4 实验。当前接口有以下限制，可能的扩展方向见[路线图](roadmap.md)。

## gRPC 与工作进程 { #1-grpc-连接与进程创建 }

同步 gRPC 和 `grpc.aio` 都使用后台线程。已有活动 gRPC 资源的进程再 fork，子进程可能继承已加锁的资源，却没有对应线程来释放它。应先创建工作进程，再在各进程内创建并连接客户端。异步客户端还需在建立连接的事件循环中使用和关闭。

批量运行实验时，可由尚未建立 gRPC 连接的父进程为每个拓扑脚本启动独立的 Python 解释器：

```python
import subprocess
import sys

subprocess.run([sys.executable, "topology.py"], check=True)
```

每个脚本结束前，用 `net.stop()` 关闭网络；在事件循环中则使用 `await net.astop()`。p4net 的清理注册表会在 fork 后的子进程中重置，这不会修复继承的 gRPC 资源。参见 [gRPC 的 fork 说明](https://github.com/grpc/grpc/blob/master/doc/fork_support.md)和[异步客户端指南](async-client.md)。

## 本机部署 { #2-仅支持单机运行 }

`Network` 在本机 Linux 系统中创建命名空间、接口和 BMv2 进程，没有远程部署或跨机器协调接口。

跨机器实验需要额外的资源管理和机器间互联。仅在多台机器上分别运行 `Network`，还不能完成这些协调工作。

## P4 架构 { #3-不支持-psa-架构 }

`Network` 使用 `v1model` 架构和 `simple_switch_grpc`。向 `Topology.add_switch()` 传入 `arch="psa"` 时，会在构造交换机描述的阶段抛出 `TopologyError`，尚未进入编译流程。

支持 PSA 需要接入匹配的编译器和交换机目标。BMv2 提供不同的[目标实现](https://github.com/p4lang/behavioral-model/blob/main/targets/README.md)，单独调整编译器调用的架构参数，不会让 `Network` 自动支持该目标。

## 拓扑变更 { #4-不支持运行中拓扑变更 }

`Topology` 是可修改的描述对象。`Network.start()` 根据它创建资源，但启动后再调用 `Topology.add_host()` 或 `Topology.add_link()`，不会创建对应的命名空间、接口或运行节点对象。`Network` 没有增删运行中节点和链路的接口。

调整拓扑时，先停止当前网络，再修改描述并创建、启动新的 `Network`。在事件循环中，应先 `await net.astop()` 完成清理，再创建新网络。通过底层运行时接口修改资源时，需要另行管理资源与拓扑描述的对应关系。

## 软件交换机的性能 { #5-bmv2-吞吐受限 }

BMv2 用于 P4 程序的开发、测试和调试。吞吐量和时延受 P4 程序、编译器与 BMv2 构建、表项、报文长度和流量模式、主机资源影响，日志、抓包和虚拟化环境也会影响结果。详见 [BMv2 性能说明](https://github.com/p4lang/behavioral-model/blob/main/docs/performance.md)。

`Link.bandwidth` 通过 Linux `tc` 配置流量整形，实际吞吐量还取决于报文处理能力和流量源。配置的链路延迟会与交换机处理时间、系统调度开销叠加。

记录转发性能时，应同时记录拓扑、程序、流量和运行环境。网络启停耗时、端到端 ping 往返时延、转发吞吐量和单台交换机的处理时延，应分别测量和报告。
