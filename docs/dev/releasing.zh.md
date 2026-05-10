---
description: 切发版的运行手册——版本校验、版本号变更、CHANGELOG、构建 wheel、TestPyPI、PyPI、打 tag、发布说明。
---

# 发版流程

切 p4net 发版的运行手册。下面以 v0.1.0 切版为示例——把
`X.Y.Z` 替换为你正在发布的版本号。

## 何时切发版

- **补丁版（`0.1.x`）**。仅向后兼容的 bug 修复。无新功能、无对
  现有用户产生意外行为变化。
- **次版本（`0.Y.0`）**。新功能，但不破坏 v0.1 API。删除或重命名
  公开符号 *不算* 次版本变更。
- **主版本（`X.0.0`）**。保留给 `1.0.0`（API 稳定承诺）以及之后
  任何破坏性变更。

不要每次提交都切发版。发版给用户提供锚点；内部重构不需要单独
发版。

## 发版前检查

在 `main` 分支的干净检出上运行：

```
git status -sb                 # 必须干净
git pull --ff-only origin main
ruff check .
ruff format --check .
mypy src/p4net
pytest
pre-commit run --all-files
```

接着运行需要额外二进制或 root 的标记套件：

```
echo "$SUDO_PASS" | sudo -S "$(which pytest)" -m integration --run-integration
pytest --run-p4c -m requires_p4c
pytest --run-bmv2 --run-p4c -m requires_bmv2
sudo -E env "PATH=$PATH" pytest \
    --run-integration --run-p4c --run-bmv2 \
    -m "integration and requires_p4c and requires_bmv2"
```

覆盖率目标（来自上面的合并运行）：

- `p4net.control` ≥ 90%
- `p4net.network` ≥ 85%
- `p4net.cli` ≥ 85%

合规性 grep 必须干净（不存在任何第三方作者归属字样）。

## 版本号变更

修改两个文件：

- `pyproject.toml`：`version = "X.Y.Z"`。
- `src/p4net/__init__.py`：`__version__ = "X.Y.Z"`。

提交：

```
git commit -am "chore: bump version to X.Y.Z"
```

## CHANGELOG

把 `## [Unreleased]` 中的内容移到新的 `## [X.Y.Z] - YYYY-MM-DD`
（UTC 日期）下，然后在最上方重新加一个空的 `## [Unreleased]`。
如果版本号提交本身很小，把 CHANGELOG 改动并入；否则单独提交：

```
git commit -am "chore: update CHANGELOG for X.Y.Z"
```

## 构建 wheel

```
pip install --upgrade build
rm -rf dist
python -m build
ls -la dist/
unzip -p dist/p4net-X.Y.Z-py3-none-any.whl p4net/__init__.py | grep __version__
```

最后一行必须打印 `__version__ = "X.Y.Z"`。否则 wheel 元数据
过时——继续之前先排查。

## TestPyPI 冒烟测试

先上传到 TestPyPI，再在新的虚拟环境里安装一遍，验证元数据与运行
时行为：

```
pip install --upgrade twine
python -m twine upload --repository testpypi dist/p4net-X.Y.Z-py3-none-any.whl

python -m venv /tmp/p4net-testpypi
source /tmp/p4net-testpypi/bin/activate
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            p4net==X.Y.Z
python -c "import p4net; print(p4net.__version__)"
```

`--extra-index-url https://pypi.org/simple/` 必须加上，让传递依赖
（`grpcio`、`prompt_toolkit` 等）从真正的 PyPI 解析，而不是从
TestPyPI 那个小得多的镜像里找。

## 发布到 PyPI

使用项目级 API token（PyPI → Account settings → API tokens →
Add API token，scope 选 "Project: p4net"）。把它作为
`__token__` 的密码存到 `~/.pypirc`，或在命令行传入：

```
python -m twine upload dist/p4net-X.Y.Z-py3-none-any.whl
```

在 `https://pypi.org/project/p4net/X.Y.Z/` 验证发布记录。

## 打 tag

只用带注解的 tag——`pip install p4net==X.Y.Z` 的用户会看到 tag
元数据，`git describe` 读取的也是它：

```
git tag -a vX.Y.Z -m "p4net X.Y.Z"
git push origin vX.Y.Z
git ls-remote --tags origin | grep vX.Y.Z
```

最后一行应该有一条记录，其反引用提交（`^{}`）与 `main` 顶端
一致。

## GitHub 发布说明

从 `CHANGELOG.md` 中抽取本次发版段落，作为 GitHub Release notes
发布：

```
gh release create vX.Y.Z \
   --notes-file <(sed -n '/## \[X.Y.Z\]/,/## \[/p' CHANGELOG.md | sed '$d')
```

如果想在 Release 页面挂上离线可装的产物，把 wheel 作为附件
传上去。

## 发版后

- 不要改回 `-dev` 标记。`pyproject.toml` 一直保持已发布版本，
  直到下一次变更。
- 如果发版后浮现问题（损坏的 sdist、错误的元数据、安装回归），
  开一个标签为 `release-followup` 的 issue，再切补丁版。不要
  撤回已发布的 wheel——在 PyPI 上把它 yank（标记为不推荐安装），
  这样已经把版本钉死的用户的 `pip install p4net==X.Y.Z` 仍然
  能工作。
- 如果新版本关闭了路线图「下一发版」中的某些条目，相应更新
  路线图。
