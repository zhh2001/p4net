---
description: p4net 仓库中的九个可运行拓扑，涵盖转发、非对称链路时延、带内遥测和多交换机并发配置。
---

# 示例

仓库的 `examples/` 目录收录了九个可运行的拓扑。先[通过 pipx 安装 CLI](../getting-started/installation.md#install-cli)，再从仓库根目录运行快速上手示例。

```sh
sudo p4net examples/quick_start/quick_start.py
```

其他示例的运行命令见各自的页面。

## 包含哪些示例

- **[快速上手（端口翻转）](quick-start.md)** —— 一台交换机上的两台主机，使用写死的端口翻转流水线。最小可运行拓扑，无需运行时表项编程。
- **[L3 转发](l3-forwarding.md)** —— 同样的两台主机，但流水线通过 Python 在启动时编程下发的 `ipv4_lpm` 表来转发。ARP 已预先注入。
- **[CPU 上送](cpu-punt.md)** —— 单主机拓扑，每个数据平面包都通过 CPU 端口上送控制器。演示 `<switch> packet send` 与 `<switch> packet listen`。
- **[双栈](dual-stack.md)** —— 两台主机各自在同一 `/24` 与 `/64` 上配置 IPv4 与 IPv6 地址。展示按接口的 IPv6 sysctl 门控。
- **[非对称链路](asymmetric-link.md)** —— 两台主机，用 `delay_a_to_b` 和 `delay_b_to_a` 按方向增加时延。
- **[IPv6 LPM](ipv6-lpm.md)** —— 两台仅 IPv6 的主机，128 位 LPM 表在运行时编程。展示 `<switch> table dump` 把 IPv6 表项渲染为人类可读形式（`fd00::1/128`）而非裸字节。
- **[INT（带内网络遥测）](int.md)** —— 单交换机 INT：流水线在每个转发包里插入一个 14 字节 shim 头，携带交换机标识、入端时间戳、出端口、队列深度与原始 etherType。原始套接字 listener 在接收端解析 shim。
- **[多跳 INT](int-multi-hop.md)** —— 两台串联交换机各自插入自己的 shim；listener 解析整条逐跳栈。更贴近生产形态的遥测拓扑；同一份 P4 二进制跑在两台交换机上，身份通过 v1.2 寄存器 API 提供。
- **[异步并发](async-concurrent.md)** —— 三台交换机两两相连，每台接入一台主机。用 `AsyncP4RuntimeClient` 并发下发 IPv4 转发表项。

## 一览

| 示例            | 主机 | 交换机 | IPv4 | IPv6 | 非对称 | CPU 上送 | 运行时表项 | 遥测   |
| --------------- | ---- | ------ | ---- | ---- | ------ | -------- | ---------- | ------ |
| quick_start     | 2    | 1      | ✓    |      |        |          |            |        |
| l3_forwarding   | 2    | 1      | ✓    |      |        |          | ✓          |        |
| cpu_punt        | 1    | 1      | ✓    |      |        | ✓        |            |        |
| dual_stack      | 2    | 1      | ✓    | ✓    |        |          |            |        |
| asymmetric_link | 2    | 1      | ✓    |      | ✓      |          |            |        |
| ipv6_lpm        | 2    | 1      |      | ✓    |        |          | ✓          |        |
| int             | 2    | 1      | ✓    |      |        |          | ✓          | ✓      |
| int_multi_hop   | 2    | 2      | ✓    |      |        |          | ✓          | ✓ (×2) |
| async_concurrent | 3   | 3      | ✓    |      |        |          | ✓          |        |

要在四台主机和两台交换机组成的拓扑中练习双栈转发、非对称时延和 PacketIn，可参考[教程](../tutorial.md)。
