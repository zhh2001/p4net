---
description: Choose a version, verify the candidate, and prepare tested wheel and source packages for release.
---

# Release process

Prepare a release from a Linux checkout with the [development and documentation dependencies](../getting-started/installation.md#install-from-source) installed. Run the commands below from the repository root in the active virtual environment. Replace `X.Y.Z` with the chosen version and keep the same shell for the build and installation steps.

## Choose a version { #when-to-cut-a-release }

Fix the release scope before final verification. Record unresolved issues and decide which ones block this release.

| Release | Use for |
| --- | --- |
| Patch, `X.Y.Z` | Compatible bug fixes. |
| Minor, `X.Y.0` | Compatible additions to the public API. |
| Major, `X.0.0` | Incompatible changes to stable APIs after the required deprecation cycle. |

Review names, signatures, documented behavior and the topology snapshot format against the previous release. Apply the [API stability rules](../api-stability.md) to each affected interface. Stable and provisional APIs retain their deprecation cycle; experimental interfaces follow their own tier. Document migration steps for affected callers.

## Verify the candidate { #pre-release-checklist }

Use a clean working tree and record the candidate commit. Check that the external tools are available, then run lint, format, type, documentation and full runtime checks:

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

All three pytest flags belong in the same run. Run Python environments sequentially because the tests share namespace and interface cleanup. Record the Python, p4c and BMv2 versions, test summary, warnings, skips and excluded tests. Check that test namespaces, interfaces and BMv2 processes have been cleaned up.

The current [CI workflow](https://github.com/zhh2001/p4net/blob/main/.github/workflows/ci.yml) covers Python 3.10–3.13 on Ubuntu 22.04 and 24.04, plus a root integration job and a package installation job. Its matrix uses the default test selection, and its root job does not install p4c or BMv2. Complete the full toolchain run above on a prepared Linux host. The documentation workflow builds strictly on Python 3.10 and 3.12. Confirm both workflows pass for the final candidate commit.

## Update version and changelog { #version-bump }

Set the same version in `pyproject.toml` (`project.version`) and `src/p4net/__init__.py` (`__version__`).

<span id="changelog"></span>

Review commits since the previous release and complete [CHANGELOG.md](../changelog.md). Group user-visible additions, changes and fixes, including compatibility notes. Put them under `## [X.Y.Z] - YYYY-MM-DD` and leave an empty `## [Unreleased]` above it. Use the local system date:

```bash
release_version=X.Y.Z
date +%F
```

Commit the release metadata and record the final revision before building the delivery packages. If code or metadata changes after verification, rerun the affected checks and rebuild from that revision.

## Build wheel and sdist { #wheel-build }

Install the build tools and create a fresh output directory. `python -m build` produces both a wheel and a source distribution; [Twine check](https://twine.readthedocs.io/en/stable/#twine-check) checks the distribution description for PyPI rendering.

```bash
set -e
python -m pip install build twine
mkdir -p dist
release_dir="$(mktemp -d "$PWD/dist/p4net-release.XXXXXX")"
python -m build --outdir "$release_dir"
python -m twine check --strict "$release_dir"/*.whl "$release_dir"/*.tar.gz
```

Check the package contents and version. The wheel must contain the library, and the sdist must include the source, documentation, examples and tests selected in `pyproject.toml`. The CI package job compares the sdist file list and contents with Git-tracked files.

## Verify both installations

Install the wheel and sdist into separate fresh environments. Check dependencies, version agreement and the console entry point from outside the source tree:

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

Repeat installation checks with the Python versions in the release validation matrix. Exercise the changed functionality through the installed package, including real p4c/BMv2 operations when those components changed. Keep the logs with the candidate revision and tool versions.

## Prepare the local handoff

Save the source revision, changelog and checksums beside the packages:

```bash
git rev-parse HEAD > "$release_dir/REVISION"
cp CHANGELOG.md "$release_dir/CHANGELOG.md"
(
  cd "$release_dir"
  sha256sum ./*.whl ./*.tar.gz > SHA256SUMS
  sha256sum --check SHA256SUMS
)
```

Deliver the wheel, sdist, `SHA256SUMS`, `REVISION`, changelog and validation summary together. Identify the tested operating system, Python/toolchain versions and any checks that were not run. Performance results should name the measured commit; a new measurement needs the same controlled setup described in the [performance guide](../performance.md).

## Public release

For a public release, use a `vX.Y.Z` tag pointing to the verified source revision. Create [GitHub release notes](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository) from the changelog and attach the tested wheel, sdist and checksum file.

Upload those same distribution files to PyPI. TestPyPI can be used to check the upload workflow first. Follow the [PyPA packaging guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/) and [Twine documentation](https://twine.readthedocs.io/en/stable/) for account setup, authentication and uploads. The installed package reports its distribution version; the Git tag identifies its source revision.

After release, record follow-up bugs and publish fixes under a new version. Update the roadmap when a release completes a listed item.
