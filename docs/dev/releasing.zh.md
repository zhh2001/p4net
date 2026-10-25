---
description: 确定版本、验证候选代码，构建并检查 wheel 和源码发布包。
---

# 发布流程 { #发版流程 }

在 Linux 检出项目，并安装[开发与文档依赖](../getting-started/installation.md#从源码安装)。以下命令在仓库根目录的虚拟环境中运行。将 `X.Y.Z` 替换为本次版本号，构建和安装验证使用同一个 shell。

## 选择版本 { #何时切发版 }

最终验证前确定发布范围，列出尚未解决的问题，明确哪些问题必须在本次发布前处理。

| 版本 | 适用变更 |
| --- | --- |
| 补丁版 `X.Y.Z` | 保持兼容的缺陷修复。 |
| 次版本 `X.Y.0` | 保持兼容的公开接口新增。 |
| 主版本 `X.0.0` | 完成弃用周期后，对稳定接口进行不兼容变更。 |

对照上一版本核对名称、签名、文档规定的行为和拓扑快照格式，按各接口的 [API 稳定性约定](../api-stability.md) 判断兼容性。稳定和暂定接口遵循各自的弃用规则，实验性接口按其分级处理。有调用方式变更时，写明迁移步骤。

## 验证候选版本 { #发版前检查 }

使用干净的工作区，记录候选提交。先确认外部工具可用，再检查代码规范、格式、类型、文档和完整运行测试：

```bash
git status --short
git rev-parse HEAD
p4c --version
simple_switch_grpc --version
python -m ruff check .
python -m ruff format --check .
python -m mypy src/p4net tools/benchmark.py
python -m mkdocs build --strict
sudo env "PATH=$PATH" "$(command -v python)" -m pytest \
  --run-integration --run-p4c --run-bmv2
```

三个 pytest 开关在同一次运行中启用。不同 Python 环境串行测试，避免共享的命名空间和接口清理相互影响。记录 Python、p4c、BMv2 版本、测试结果、警告、跳过及未选中的用例，并检查测试命名空间、接口和 BMv2 进程已回收。

当前 [CI 工作流](https://github.com/zhh2001/p4net/blob/main/.github/workflows/ci.yml) 覆盖 Ubuntu 22.04、24.04 上的 Python 3.10～3.13，另有 root 集成测试和安装包检查。版本矩阵运行默认测试集，root 任务没有安装 p4c、BMv2，因此完整工具链测试仍需在准备好的 Linux 环境中执行。文档工作流使用 Python 3.10、3.12 严格构建。最终候选提交的两个工作流都应通过。

## 更新版本与变更日志 { #版本号变更 }

将 `pyproject.toml` 的 `project.version` 和 `src/p4net/__init__.py` 的 `__version__` 设为同一版本。

<span id="changelog"></span>

检查上一版本以来的提交，补齐 [CHANGELOG.md](../changelog.md)，按新增、变更和修复整理用户可见的内容，并说明兼容性影响。将本次内容放在 `## [X.Y.Z] - YYYY-MM-DD` 下，顶部保留空的 `## [Unreleased]`。日期以本机系统时间为准：

```bash
release_version=X.Y.Z
date +%F
```

提交版本信息后，记录最终提交，再构建交付包。验证后若代码或元数据发生变化，重新运行受影响的检查，并从该提交重新构建。

## 构建 wheel 和源码包 { #构建-wheel }

安装构建工具，创建独立的输出目录。`python -m build` 同时生成 wheel 和源码包，[Twine check](https://twine.readthedocs.io/en/stable/#twine-check) 检查包的描述能否在 PyPI 正确渲染。

```bash
set -e
python -m pip install build twine
mkdir -p dist
release_dir="$(mktemp -d "$PWD/dist/p4net-release.XXXXXX")"
python -m build --outdir "$release_dir"
python -m twine check --strict "$release_dir"/*.whl "$release_dir"/*.tar.gz
```

核对包内容和版本。wheel 应包含库代码，源码包应包含 `pyproject.toml` 选定的源码、文档、示例和测试。CI 的安装包任务会将源码包的文件清单和内容与 Git 已跟踪文件逐一比对。

## 分别验证两种安装方式

将 wheel 和源码包安装到两个全新环境，检查依赖、版本一致性和命令行入口。导入与命令行检查在源码目录之外执行：

```bash
smoke_dir="$(mktemp -d "${TMPDIR:-/tmp}/p4net-install.XXXXXX")"
for kind in wheel sdist; do
  env_path="$smoke_dir/$kind"
  python -m venv "$env_path"
  if [ "$kind" = wheel ]; then
    "$env_path/bin/python" -m pip install "$release_dir"/*.whl
  else
    "$env_path/bin/python" -m pip install "$release_dir"/*.tar.gz
  fi
  "$env_path/bin/python" -m pip check
  (
    cd "$smoke_dir"
    "$env_path/bin/python" -I - "$release_version" <<'PY'
import importlib.metadata
import sys

import p4net

assert importlib.metadata.version("p4net") == p4net.__version__ == sys.argv[1]
print(p4net.__version__, p4net.__file__)
PY
    "$env_path/bin/p4net" --help
  )
done
```

在本次验证矩阵的各 Python 版本中重复安装检查，并通过已安装的包验证本次变更涉及的功能。涉及编译或交换机运行时，要覆盖真实 p4c、BMv2 操作。日志与候选提交、工具版本一同保存。

## 整理本地交付包

将源码提交、变更日志和校验文件保存在发布包目录：

```bash
git rev-parse HEAD > "$release_dir/REVISION"
cp CHANGELOG.md "$release_dir/CHANGELOG.md"
(
  cd "$release_dir"
  sha256sum ./*.whl ./*.tar.gz > SHA256SUMS
  sha256sum --check SHA256SUMS
)
```

交付 wheel、源码包、`SHA256SUMS`、`REVISION`、变更日志和验证摘要，注明测试系统、Python 与工具链版本，以及未执行的检查。性能结果应标明实际测量的提交；需要重新采集时，按[性能指南](../performance.md)准备稳定的测量环境。

## 公开发布

公开发布时，用 `vX.Y.Z` 标签标识已验证的源码提交。根据变更日志撰写 [GitHub 发布说明](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)，附上已测试的 wheel、源码包和校验文件。

将同一批发布文件上传到 PyPI，也可以先通过 TestPyPI 验证上传流程。账户、认证和上传步骤参考 [PyPA 打包指南](https://packaging.python.org/en/latest/tutorials/packaging-projects/)与 [Twine 文档](https://twine.readthedocs.io/en/stable/)。安装后的包显示发行版本，Git 标签用于定位源码提交。

发布后记录新增问题，用新版本交付后续修复。完成路线图中的事项时，同步更新路线图。
