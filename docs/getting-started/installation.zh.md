---
description: 安装 p4net 与其依赖的外部组件（p4c、BMv2 simple_switch_grpc、graphviz）的步骤。
---

# 安装

p4net 是一层薄薄的 Python 封装，建立在 `p4c`、`simple_switch_grpc`
（BMv2）以及 Linux 内核的网络命名空间和流量控制原语之上。安装
Python 包是简单的部分；把外部编译器和数据平面准备就绪才是主要工作量。

## 系统要求

- **Linux**，内核 ≥ 5.4。已在 Ubuntu 22.04 与 24.04 上验证；其他
  现代发行版应当也能工作。
- **Python ≥ 3.10**。CI 覆盖 3.10–3.13。
- **root 权限**用于运行编排器。p4net 需要 `CAP_NET_ADMIN` 来创建
  网络命名空间和 veth 对、运行 `tc qdisc`。直接 `sudo` 运行最简单；
  对 Python 解释器执行 `setcap cap_net_admin+ep` 也可以，但需要注意
  shebang 查找等常见副作用。

## 必需的外部工具

| 工具                    | 用途                                                          | 安装提示                               |
| ----------------------- | ------------------------------------------------------------- | -------------------------------------- |
| `p4c`                   | P4_16 v1model 编译器。                                        | `apt install p4lang-p4c`（p4lang OBS 仓库），或[从源码构建](https://github.com/p4lang/p4c)。 |
| `simple_switch_grpc`    | 启用了 P4Runtime gRPC 服务的 BMv2 数据平面。                  | 从 [`behavioral-model`](https://github.com/p4lang/behavioral-model) 构建，配置开启 `--with-pi` 与 `--with-thrift`。 |
| `iproute2`（`ip`、`tc`）| 命名空间、链路、地址、qdisc 管理。                            | 现代发行版默认已安装。                 |
| `graphviz`（`dot`）     | 可选。仅在使用 `topology graph` 渲染时需要。                  | `apt install graphviz`。               |

确认每一个都能在 `PATH` 上找到：

```
$ p4c --version
$ simple_switch_grpc --version
$ ip -V
$ tc -V
$ dot -V        # 可选
```

任何一个返回 "command not found"，请先把它安装好再继续。p4net 启动时
会给出清晰的错误信息，但提前定位会快得多。

## 从 PyPI 安装 p4net

推荐在虚拟环境里安装：

```
python3 -m venv .venv
. .venv/bin/activate
pip install p4net
```

可选附加组件：

- `pip install 'p4net[dev]'` —— 测试、代码风格、类型检查工具
  （`pytest`、`pytest-cov`、`pytest-mock`、`ruff`、`mypy`、
  `pre-commit`）。
- `pip install 'p4net[docs]'` —— 用于本地构建本文档站点
  （`mkdocs`、`mkdocs-material`、`mkdocstrings`、
  `mkdocs-static-i18n`）。

## 从源码安装（开发）

```
git clone https://github.com/zhh2001/p4net
cd p4net
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

运行单元测试：

```
pytest -q
```

集成测试需要 root 权限和外部工具，需要显式启用：

```
sudo -E env "PATH=$PATH" pytest --run-integration --run-p4c --run-bmv2
```

## 验证

```
$ python -c "import p4net; print(p4net.__version__)"
0.2.0

$ p4net --help
usage: p4net [-h] [--no-shell] [--unsafe] [--log-dir LOG_DIR] ...
```

如果在 `sudo` 下 `p4net --help` 提示 `command not found`，多半是因为
sudoers 的 `secure_path` 把虚拟环境从 `PATH` 中剥离了。两种绕法：

```
sudo env "PATH=$PATH" p4net <topology.py>           # 显式传 env
sudo "$(. .venv/bin/activate && which p4net)" ...   # 绝对路径
```

## 常见问题

最常见的几类问题——缺少命名空间工具、sysctl 权限错误、BMv2 启动卡住、
gRPC 版本不一致——都在[故障排查](../troubleshooting.md)中有专门条目。
如果遇到那里没有覆盖的情况，请提交 issue。
