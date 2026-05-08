"""Shared pytest fixtures and integration-test gating."""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run integration tests that require root and Linux network namespaces",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    if not config.getoption("--run-integration"):
        deselected = [item for item in items if "integration" in item.keywords]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = [item for item in items if "integration" not in item.keywords]
        return
    if os.geteuid() != 0:
        skip_root = pytest.mark.skip(reason="integration tests require root")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_root)
