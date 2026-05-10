# Release process

Runbook for cutting a p4net release. The v0.1.0 cutover is the worked
example throughout — substitute `X.Y.Z` for the version you're shipping.

## When to cut a release

- **Patch (`0.1.x`).** Backwards-compatible bug fixes only. No new
  features, no behavior changes that would surprise an existing user.
- **Minor (`0.Y.0`).** New features that don't break v0.1 APIs. A
  removed or renamed public symbol is *not* a minor change.
- **Major (`X.0.0`).** Reserved for `1.0.0` (API stability commitment)
  and for any future breaking change after that.

Don't release every commit. Releases are for users to peg against;
internal refactors do not need their own release.

## Pre-release checklist

Run from a clean checkout on `main`:

```
git status -sb                 # must be clean
git pull --ff-only origin main
ruff check .
ruff format --check .
mypy src/p4net
pytest
pre-commit run --all-files
```

Then the marker suites that need extra binaries or root:

```
echo "$SUDO_PASS" | sudo -S "$(which pytest)" -m integration --run-integration
pytest --run-p4c -m requires_p4c
pytest --run-bmv2 --run-p4c -m requires_bmv2
sudo -E env "PATH=$PATH" pytest \
    --run-integration --run-p4c --run-bmv2 \
    -m "integration and requires_p4c and requires_bmv2"
```

Coverage targets (from the same combined run):

- `p4net.control` ≥ 90%
- `p4net.network` ≥ 85%
- `p4net.cli` ≥ 85%

Compliance grep must be clean (no AI/LLM/tool attribution anywhere).

## Version bump

Edit two files:

- `pyproject.toml`: `version = "X.Y.Z"`.
- `src/p4net/__init__.py`: `__version__ = "X.Y.Z"`.

Commit:

```
git commit -am "chore: bump version to X.Y.Z"
```

## CHANGELOG

Move the contents of `## [Unreleased]` under a new section
`## [X.Y.Z] - YYYY-MM-DD` (UTC date), then re-add an empty
`## [Unreleased]` section above. If the version-bump commit is tiny,
fold the CHANGELOG edit into it; otherwise:

```
git commit -am "chore: update CHANGELOG for X.Y.Z"
```

## Wheel build

```
pip install --upgrade build
rm -rf dist
python -m build
ls -la dist/
unzip -p dist/p4net-X.Y.Z-py3-none-any.whl p4net/__init__.py | grep __version__
```

The last line must print `__version__ = "X.Y.Z"`. If not, the wheel
metadata is stale — investigate before continuing.

## TestPyPI smoke

Upload to TestPyPI first, then install into a fresh venv to verify
metadata and runtime behavior:

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

`--extra-index-url https://pypi.org/simple/` is required so transitive
deps (`grpcio`, `prompt_toolkit`, etc.) resolve from real PyPI rather
than from TestPyPI's much smaller mirror.

## PyPI publish

Use a project-scoped API token (PyPI → Account settings → API tokens →
Add API token, scope = "Project: p4net"). Store it as the password to
`__token__` in `~/.pypirc` or pass it inline:

```
python -m twine upload dist/p4net-X.Y.Z-py3-none-any.whl
```

Verify the listing at `https://pypi.org/project/p4net/X.Y.Z/`.

## Tag

Annotated tags only — `pip install p4net==X.Y.Z` consumers see the tag's
metadata, and `git describe` reads it:

```
git tag -a vX.Y.Z -m "p4net X.Y.Z"
git push origin vX.Y.Z
git ls-remote --tags origin | grep vX.Y.Z
```

The last line should show one entry whose dereferenced commit (`^{}`)
matches the tip of `main`.

## GitHub release notes

Extract the section from `CHANGELOG.md` and post it as the GitHub
release notes:

```
gh release create vX.Y.Z \
   --notes-file <(sed -n '/## \[X.Y.Z\]/,/## \[/p' CHANGELOG.md | sed '$d')
```

Attach the wheel as a release asset if you want offline-installable
artifacts on the release page.

## Post-release

- No version bump back to a `-dev` marker. `pyproject.toml` stays at the
  released version until the next bump.
- If post-release problems surface (broken sdist, wrong metadata,
  installer regression), open an issue tagged `release-followup` and
  cut a patch release. Do not retract published wheels — yank them on
  PyPI instead, so `pip install p4net==X.Y.Z` keeps working for users
  who already pinned.
- Update the roadmap's "next release" section if the new release closed
  out items listed there.
