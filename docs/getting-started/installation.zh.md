---
description: 安装 Linux 工具和 p4net 命令行，或在 Python 项目中安装 p4net。
---

# 安装

先安装 Linux 工具。跟随命令行教程时，通过 pipx 全局安装 p4net，即可直接运行 `sudo p4net`。p4net 的 Python 包不包含 P4 编译器和 BMv2。

## 系统要求

- Linux，内核版本不低于 5.4。CI 在 Ubuntu 22.04 和 24.04 上运行。
- Python 3.10 或更高版本。CI 覆盖 Python 3.10–3.13。
- 运行网络需要 root 权限，包括创建网络命名空间、配置链路和启动交换机。定义拓扑和编译 P4 程序可以使用普通账号。

## 安装外部工具 { #required-external-tools }

在 Ubuntu 上，先安装 Python 环境工具和 Linux 网络工具：

```bash
sudo apt update
sudo apt install python3-venv python3-pip iproute2 iputils-ping procps git
```

然后按照上游文档安装 P4 编译器和 BMv2：

- **p4c**：参照[软件包安装说明](https://github.com/p4lang/p4c#installing-packaged-versions-of-p4c)，也可以从源码构建。安装结果需要包含 BMv2 后端，以及 `core.p4` 和 `v1model.p4` 头文件。
- **BMv2**：参照 [`simple_switch_grpc` 构建说明](https://github.com/p4lang/behavioral-model/tree/main/targets/simple_switch_grpc#dependencies-and-build-steps)。该目标需要支持 Protobuf/gRPC 的 PI，构建 BMv2 时需启用 `--with-pi`。

p4net 使用以下命令：

| 命令 | 用途 | 所属软件包或组件 |
| --- | --- | --- |
| `p4c` | 编译 P4_16 v1model 程序 | 含 BMv2 后端的 p4c |
| `simple_switch_grpc` | 运行交换机并提供 P4Runtime 服务 | BMv2 |
| `ip`、`tc` | 管理命名空间、链路、地址和流量控制 | `iproute2` |
| `ping` | 在主机中检查连通性 | `iputils-ping` |
| `sysctl` | 配置命名空间中的网络参数 | `procps` |

确认这些命令均可通过 `PATH` 找到：

```bash
p4c --version
simple_switch_grpc --version
ip -V
tc -V
ping -V
sysctl --version
```

以下两项功能需要额外工具：

- 将拓扑渲染为 SVG、PNG 或 PDF 需要 Graphviz，可通过 `sudo apt install graphviz` 安装。仅导出 DOT 文件不需要 Graphviz。
- 读取、写入或重置寄存器需要 `simple_switch_CLI` 和 BMv2 的 Thrift 服务。如需操作寄存器，构建 BMv2 时保留 Thrift 支持。

## 通过 pipx 安装 CLI { #install-cli }

Ubuntu 用户可以通过系统软件包安装 pipx。其他发行版请参照 [pipx 安装指南](https://pipx.pypa.io/stable/how-to/install-pipx.html)，并确认可以运行 `sudo pipx --version`。

```bash
sudo apt install pipx
sudo pipx --version
```

pipx 1.6.0 及以上版本可以把 `--global` 放在 `install` 后面。

```bash
sudo pipx install --global p4net
```

Ubuntu 24.04 默认软件源提供的是 pipx 1.4.3。使用这类较早版本时，可以显式指定相同的[全局安装目录](https://pipx.pypa.io/stable/how-to/configure-paths.html#install-for-all-users)。

```bash
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin \
  PIPX_MAN_DIR=/usr/local/share/man pipx install p4net
```

## 验证安装

运行帮助命令，确认 `sudo` 能找到并启动 p4net。

```bash
sudo p4net --help
```

pipx 默认将命令入口放在 `/usr/local/bin`，将 p4net 和它的 Python 依赖安装到 `/opt/pipx` 下的独立环境。看到帮助后，继续阅读[快速上手](quickstart.md)。

## 在项目环境中安装 { #project-environments }

如果要在自己的 Python 项目中导入 p4net，或为拓扑脚本使用额外的 Python 依赖，可以将 p4net 安装到项目的虚拟环境中。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install p4net
```

跟随命令行示例时，先激活该环境，再用它的 p4net 入口启动拓扑。

```bash
sudo "$(command -v p4net)" quickstart.py
```

`command -v` 在 `sudo` 切换用户前找到当前环境中的命令入口。也可以运行 `sudo "$(command -v python)" -m p4net quickstart.py`。

## 从源码安装

开发项目时，克隆仓库并以可编辑模式安装，同时安装测试、代码规范和类型检查工具：

```bash
git clone https://github.com/zhh2001/p4net.git
cd p4net
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

从这个源码环境启动拓扑时，请使用[当前项目环境的命令入口](#project-environments)。

文档通过 [CairoSVG](https://cairosvg.org/documentation/) 生成分享图片。在 Ubuntu 上，先用 `sudo apt install libcairo2` 安装系统 Cairo 库，再在仓库根目录安装文档工具：

```bash
python -m pip install -e '.[dev,docs]'
```

## 运行测试和构建文档

从源码安装后，在仓库根目录运行测试：

```bash
python -m pytest
```

该命令只运行默认测试集，不包含标记为需要 root、p4c 或 BMv2 的测试。安装外部工具后，通过以下命令启用这三组测试：

```bash
sudo env "PATH=$PATH" "$(command -v python)" -m pytest \
  --run-integration --run-p4c --run-bmv2
```

安装文档工具后，通过以下命令构建站点：

```bash
python -m mkdocs build --strict
```

安装或启动时遇到问题，可查阅[故障排查](../troubleshooting.md)。
