---
description: p4net 如何使用标准库 logging 模块——命名空间层次与级别约定。
---

# 日志

p4net 使用标准库 `logging` 模块，根 logger 为 `p4net`。每个内部
模块按自己的点分路径打日志——`p4net.network.orchestrator`、
`p4net.runtime.bmv2`、`p4net.compiler.p4c` 等等——使用方可以独立
过滤或转发任何子系统。

## 命名空间

`p4net.*` logger 命名空间是**稳定**的：1.x 内不会改名。具体含义：

- `logging.getLogger("p4net.network").setLevel(logging.DEBUG)` 在
  1.0 能用，并且在所有 1.x 发布中都将继续可用。
- 可能会新增子模块名（如 `p4net.network.cleanup`），但已有名字
  不会在没有大版本升级的情况下被改名或删除。

## 级别

| 级别    | 你会看到什么 |
| ------- | ------------ |
| ERROR   | 库无法处理的失败。通常伴随抛出异常。 |
| WARNING | 意外但已处理。库自己恢复或绕过了某个状况。 |
| INFO    | 生命周期事件：`Network.start` / `stop`、BMv2 拉起、gRPC 连接、主控权获取。 |
| DEBUG   | 详细决策：编译器缓存命中/未命中、重试、election ID、底层 netlink 调用。 |

某个事件应该走哪个级别**不属于**稳定契约——如果某个 INFO 在实际
使用中太吵，我们可能会降到 DEBUG，反之亦然。**级别分类法**本身
是稳定的，**事件具体走哪个级别**不是。

**日志文本本身不稳定**。不要去 grep 生产日志里的特定字符串；用
级别与 logger 名筛选。

## CLI 详细度

`p4net` 命令行接受：

- 不带参 → 默认 `WARNING`（只显示问题）
- `-v` / `--verbose` → `INFO`
- `-vv` → `DEBUG`

更高的（`-vvv`……）也都解析为 `DEBUG`；没有比默认 `WARNING` 更
安静的级别。

## 代码内配置

在自己的脚本里，请在导入或实例化 p4net 组件之前先配好 `logging`
——`basicConfig` 只会对第一次调用生效：

```python
import logging

logging.basicConfig(level=logging.INFO)

# 可选：让某一个子系统跑到 DEBUG，其它保持 INFO。
logging.getLogger("p4net.runtime").setLevel(logging.DEBUG)
```

更复杂的配置（文件 handler、JSON 格式化器、转发到 syslog）
请用 `logging.config.dictConfig`，与处理其它库一致。
