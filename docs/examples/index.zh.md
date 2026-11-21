---
description: p4net 仓库中的九个可运行拓扑，涵盖转发、非对称链路时延、带内遥测和多交换机并发配置。
---

# 示例

仓库的 `examples/` 目录收录了九个可运行的拓扑。先[安装 P4 编译器、BMv2 等外部工具](../getting-started/installation.md#required-external-tools)，再[通过 pipx 安装 CLI](../getting-started/installation.md#install-cli)。

用下面的命令克隆仓库并启动快速上手示例。已有仓库副本时，在仓库根目录执行最后一条命令。

```sh
git clone https://github.com/zhh2001/p4net.git
cd p4net
sudo p4net examples/quick_start/quick_start.py
```

其他示例的运行命令见各自的页面。

## 包含哪些示例

- **[快速上手（端口翻转）](quick-start.md)** 在交换机的端口 1 和端口 2 之间转发报文。
- **[L3 转发](l3-forwarding.md)** 用 Python 下发 IPv4 路由，并验证最长前缀匹配。
- **[CPU 上送](cpu-punt.md)** 演示通过 CPU 端口接收 PacketIn 和发送 PacketOut。
- **[双栈](dual-stack.md)** 为两台主机配置 IPv4 和 IPv6 地址，并验证两种协议的连通性。
- **[非对称链路](asymmetric-link.md)** 查看各方向的时延如何影响 ping 的往返时间。
- **[IPv6 LPM](ipv6-lpm.md)** 下发 IPv6 路由，并在 `table dump` 中查看 `fd00::1/128` 这样的前缀。
- **[INT（带内网络遥测）](int.md)** 由一台交换机插入遥测头，再由接收端读取。
- **[多跳 INT](int-multi-hop.md)** 查看两台串联交换机分别插入的遥测数据。
- **[异步并发](async-concurrent.md)** 并发配置三台交换机的 IPv4 转发表。

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
