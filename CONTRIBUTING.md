<!-- --8<-- [start:contribution-overview] -->
# Contributing to p4net

Thanks for your interest in contributing.

## Discussion first

For non-trivial changes, please open an issue to discuss the design before
sending a large pull request. Small fixes and clearly-scoped improvements can
go straight to a PR.
<!-- --8<-- [end:contribution-overview] -->

## Development setup

Clone the repository and install p4net with the development tools in a
virtual environment. On Ubuntu, install Git with `sudo apt install git`
if it is missing.

```sh
git clone https://github.com/zhh2001/p4net.git
cd p4net
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pre-commit install
```

To start a topology from this checkout, use the
[CLI entry point from your active virtual environment](https://zhh2001.github.io/p4net/getting-started/installation/#project-environments).

## Before submitting

Run these checks from the repository root with the virtual environment active.

```sh
python -m ruff check .
python -m ruff format --check .
python -m mypy src/p4net
python -m pytest
```

### Integration tests

The default test run leaves out tests that require root privileges, p4c, or
BMv2. Install the [external tools](https://zhh2001.github.io/p4net/getting-started/installation/#required-external-tools)
and run pytest with all three options to include those tests.

```sh
sudo env "PATH=$PATH" "$(command -v python)" -m pytest \
  --run-integration --run-p4c --run-bmv2
```

### CI matrix

The [CI workflow](https://github.com/zhh2001/p4net/blob/main/.github/workflows/ci.yml)
runs the default test set with Python 3.10–3.13 on Ubuntu 22.04 and 24.04.
A separate job runs the network namespace integration tests as root.

## Build the documentation

The site build uses Cairo and Pango for social cards. On Ubuntu, install the
system libraries first, then install the documentation dependencies in the
active virtual environment and build the site from the repository root.

```sh
sudo apt install libcairo2 libpango-1.0-0 libpangoft2-1.0-0
python -m pip install -e '.[docs]'
python -m mkdocs build --strict
```

<!-- --8<-- [start:commit-messages] -->
## Commit messages

Conventional-commit style subjects are preferred:

```
type: subject
```

Allowed types: `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `ci`,
`build`.
<!-- --8<-- [end:commit-messages] -->
