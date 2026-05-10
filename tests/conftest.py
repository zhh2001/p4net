"""Shared pytest fixtures and gating for marker-restricted test suites."""

from __future__ import annotations

import os

# The bundled `p4runtime` proto stubs were generated against an older protoc
# and do not load under modern `google.protobuf` C++ descriptors. Set the
# python-impl env var here, before any test file imports `p4.config.v1.*`,
# so that test collection itself does not crash.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

import shutil
import subprocess

import pytest

from tests._artifact_patterns import TEST_IFACE_PATTERN, TEST_NS_PATTERN


def _list_namespaces() -> list[str]:
    try:
        out = subprocess.check_output(["ip", "netns", "list"], text=True)
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    names: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        names.append(line.split()[0])
    return names


def _list_interfaces() -> list[str]:
    try:
        out = subprocess.check_output(["ip", "-br", "link", "show"], text=True)
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    names: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        # Drop a trailing "@peer" suffix (e.g. "vethA@if13" -> "vethA").
        token = parts[0].split("@", 1)[0]
        if token:
            names.append(token)
    return names


def _purge_stale_test_artifacts() -> None:
    for name in _list_namespaces():
        if TEST_NS_PATTERN.match(name):
            subprocess.run(["ip", "netns", "delete", name], capture_output=True, check=False)
    for iface in _list_interfaces():
        if TEST_IFACE_PATTERN.match(iface):
            subprocess.run(["ip", "link", "delete", iface], capture_output=True, check=False)


@pytest.fixture(scope="session", autouse=True)
def _purge_stale_artifacts() -> object:
    """Delete stale test namespaces and veth pairs at session start AND end.

    Best-effort; runs without root will fail every `ip ... delete` silently
    via `check=False` and the suite proceeds. Tests that need real cleanup
    still own their own try/finally blocks — this fixture is a safety net
    against partial failures from earlier runs.
    """
    _purge_stale_test_artifacts()
    yield
    _purge_stale_test_artifacts()


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
    parser.addoption(
        "--run-bmv2",
        action="store_true",
        default=False,
        help="run tests that require the simple_switch_grpc binary on PATH",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    _gate_integration(config, items)
    _gate_requires_p4c(config, items)
    _gate_requires_bmv2(config, items)


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


def _gate_requires_bmv2(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-bmv2"):
        deselected = [item for item in items if "requires_bmv2" in item.keywords]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = [item for item in items if "requires_bmv2" not in item.keywords]
        return
    if shutil.which("simple_switch_grpc") is None:
        skip_no_bmv2 = pytest.mark.skip(
            reason="requires_bmv2 tests need 'simple_switch_grpc' on PATH"
        )
        for item in items:
            if "requires_bmv2" in item.keywords:
                item.add_marker(skip_no_bmv2)
