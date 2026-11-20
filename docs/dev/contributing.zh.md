---
description: 搭建 p4net 开发环境、运行检查、构建文档及提交变更。
---

# 贡献指南

--8<-- "CONTRIBUTING.md:contribution-overview"

## 开发环境 { #development-setup }

克隆仓库，创建虚拟环境，再以可编辑模式安装 p4net 和开发工具。如果 Ubuntu 上还没有 Git，可以用 `sudo apt install git` 安装。

```sh
git clone https://github.com/zhh2001/p4net.git
cd p4net
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pre-commit install
```

从这个环境启动拓扑时，请使用[当前虚拟环境中的 p4net 命令](../getting-started/installation.md#project-environments)。

## 提交前检查 { #before-submitting }

激活虚拟环境后，在仓库根目录运行以下检查。

```sh
python -m ruff check .
python -m ruff format --check .
python -m mypy src/p4net
python -m pytest
```

### 集成测试 { #integration-tests }

默认测试集会排除需要 root 权限、p4c 或 BMv2 的测试。[安装外部工具](../getting-started/installation.md#required-external-tools)后，用以下命令一并运行这些测试。

```sh
sudo env "PATH=$PATH" "$(command -v python)" -m pytest \
  --run-integration --run-p4c --run-bmv2
```

### CI 测试矩阵 { #ci-matrix }

[CI 工作流](https://github.com/zhh2001/p4net/blob/main/.github/workflows/ci.yml)在 Ubuntu 22.04 和 24.04 上用 Python 3.10–3.13 运行默认测试集。另有一个任务以 root 权限运行网络命名空间集成测试。

## 构建文档 { #build-the-documentation }

文档站使用 Cairo 和 Pango 生成分享图片。在 Ubuntu 上先安装系统库，再在已激活的虚拟环境中安装文档依赖，并从仓库根目录构建。

```sh
sudo apt install libcairo2 libpango-1.0-0 libpangoft2-1.0-0
python -m pip install -e '.[docs]'
python -m mkdocs build --strict
```

--8<-- "CONTRIBUTING.md:commit-messages"
