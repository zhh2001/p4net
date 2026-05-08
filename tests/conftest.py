"""Shared pytest fixtures and gating for marker-restricted test suites."""

from __future__ import annotations

import os
import shutil

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run integration tests that require root and Linux network namespaces",
    )
    parser.addoption(
        "--run-p4c",
        action="store_true",
        default=False,
        help="run tests that require the 'p4c' compiler binary on PATH",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    _gate_integration(config, items)
    _gate_requires_p4c(config, items)


def _gate_integration(config: pytest.Config, items: list[pytest.Item]) -> None:
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


def _gate_requires_p4c(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-p4c"):
        deselected = [item for item in items if "requires_p4c" in item.keywords]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = [item for item in items if "requires_p4c" not in item.keywords]
        return
    if shutil.which("p4c") is None:
        skip_no_p4c = pytest.mark.skip(reason="requires_p4c tests need 'p4c' on PATH")
        for item in items:
            if "requires_p4c" in item.keywords:
                item.add_marker(skip_no_p4c)
