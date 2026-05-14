"""Smoke tests for the package."""

import p4net


def test_version() -> None:
    assert p4net.__version__ == "1.6.0"
