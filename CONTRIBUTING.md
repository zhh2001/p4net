# Contributing to p4net

Thanks for your interest in contributing.

## Discussion first

For non-trivial changes, please open an issue to discuss the design before
sending a large pull request. Small fixes and clearly-scoped improvements can
go straight to a PR.

## Development setup

After cloning the repository:

```
pip install -e '.[dev]' && pre-commit install
```

## Before submitting

Run the full local check suite and make sure it is clean:

```
ruff check . && ruff format --check . && mypy src/p4net && pytest
```

## Commit messages

Conventional-commit style subjects are preferred:

```
type: subject
```

Allowed types: `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `ci`,
`build`.
