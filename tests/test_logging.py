"""Smoke tests for the stability of the `p4net.*` logger namespace."""

from __future__ import annotations

import importlib
import logging

import pytest

_SUBPACKAGE_LOGGER_NAMES = [
    "p4net",
    "p4net.cli",
    "p4net.compiler",
    "p4net.control",
    "p4net.network",
    "p4net.runtime",
    "p4net.topo",
]


@pytest.mark.parametrize("name", _SUBPACKAGE_LOGGER_NAMES)
def test_subpackage_logger_resolves(name: str) -> None:
    assert isinstance(logging.getLogger(name), logging.Logger)


@pytest.mark.parametrize(
    "module_name",
    [
        "p4net.network.orchestrator",
        "p4net.runtime.bmv2",
        "p4net.runtime.netns",
        "p4net.runtime.link",
        "p4net.control.client",
        "p4net.compiler.p4c",
    ],
)
def test_module_uses_dotted_logger_name(module_name: str) -> None:
    module = importlib.import_module(module_name)
    logger_obj = getattr(module, "logger", None) or getattr(module, "_log", None)
    assert logger_obj is not None, f"{module_name} exports no module logger"
    assert isinstance(logger_obj, logging.Logger)
    assert logger_obj.name == module_name
