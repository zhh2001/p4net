---
description: 在命令行和 Python 脚本中配置 P4Net 日志。
---

# 日志

P4Net 使用 Python 标准库的 `logging` 模块。各个日志器都归在 `p4net` 下，可以统一配置，也可以单独调整一个子系统。

## 日志器名称

`p4net` 日志器以及 `p4net.topo`、`p4net.network`、`p4net.control`、`p4net.runtime`、`p4net.compiler` 和 `p4net.cli` 子树在 1.x 内保持稳定。长期使用的日志过滤规则和级别设置可以基于这些名称。具体模块的日志器名称、日志文本和单个事件的日志级别可能调整。详见 [API 稳定性约定](api-stability.md#日志命名空间)。

## 命令行日志级别

| 参数 | Python 日志最低级别 |
| --- | --- |
| 默认 | `WARNING` |
| `-v` 或 `--verbose` | `INFO` |
| `-vv`、`-vvv` 等 | `DEBUG` |

用 `-v` 可以查看网络启停和 P4Runtime 连接日志。用 `-vv` 还可以查看编译缓存是否命中等细节。在仓库根目录运行以下示例。

```sh
sudo p4net -v examples/quick_start/quick_start.py
```

在交互模式下，`Ready` 和 `Stopping topology...` 等启停提示会在各日志级别下显示。用 `--log-dir DIR` 可以指定 BMv2 自己的日志文件目录。

## 在 Python 脚本中配置

在启动网络前配置 Python 日志。下面把默认级别设为 `INFO`，并单独打开 `p4net.runtime` 子树的 `DEBUG` 日志。

```python
import logging

logging.basicConfig(level=logging.INFO)

logging.getLogger("p4net.runtime").setLevel(logging.DEBUG)
```

如果应用已有日志配置，请在原配置中调整日志器级别，并让对应的处理器接收 `DEBUG` 日志。
