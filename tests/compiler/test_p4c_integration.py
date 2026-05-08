"""Integration tests that exercise a real `p4c` compile pipeline.

Gated by the `requires_p4c` marker (see `tests/conftest.py`); skipped by
default. Run with `pytest --run-p4c -m requires_p4c`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from p4net.compiler import CompileError, P4Compiler

pytestmark = pytest.mark.requires_p4c

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "p4"
_FORWARD = _FIXTURES / "forward.p4"
_SYNTAX_ERROR = _FIXTURES / "syntax_error.p4"


def test_real_compile_succeeds(tmp_path: Path) -> None:
    c = P4Compiler(cache_dir=tmp_path / "cache")
    result = c.compile(_FORWARD)
    assert result.cache_hit is False
    assert result.bmv2_json.is_file()
    assert result.p4info.is_file()
    assert result.bmv2_json.stat().st_size > 0
    assert result.p4info.stat().st_size > 0
    assert result.compiler_version  # non-empty version banner
    assert result.bmv2_json.parent == tmp_path / "cache" / result.source_hash


def test_real_compile_cache_hit_skips_subprocess(tmp_path: Path, mocker: MockerFixture) -> None:
    c = P4Compiler(cache_dir=tmp_path / "cache")
    first = c.compile(_FORWARD)
    assert first.cache_hit is False

    # Patch subprocess.run AFTER the first compile so the cache is populated;
    # any further call would be a bug.
    sentinel = MagicMock(side_effect=AssertionError("subprocess.run called on cache hit"))
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=sentinel)

    second = c.compile(_FORWARD)
    assert second.cache_hit is True
    assert second.source_hash == first.source_hash
    assert second.bmv2_json == first.bmv2_json
    sentinel.assert_not_called()


def test_real_compile_force_rebuilds_cache(tmp_path: Path) -> None:
    c = P4Compiler(cache_dir=tmp_path / "cache")
    first = c.compile(_FORWARD)
    second = c.compile(_FORWARD, force=True)
    assert second.cache_hit is False
    assert second.source_hash == first.source_hash
    # The cache directory still exists and contains the canonical files.
    assert second.bmv2_json.is_file()
    assert second.p4info.is_file()


def test_real_compile_syntax_error_raises_and_leaves_no_cache_entry(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    c = P4Compiler(cache_dir=cache_dir)
    with pytest.raises(CompileError) as info:
        c.compile(_SYNTAX_ERROR)
    err = info.value
    assert err.returncode != 0
    assert err.stderr.strip()  # non-empty
    # The cache_dir may or may not exist; if it does, no hash directory for
    # this failed compile should have been created.
    if cache_dir.exists():
        entries = [p.name for p in cache_dir.iterdir() if p.is_dir()]
        # No staging or final entries should remain.
        assert not any(name.endswith(".tmp-" + str(__import__("os").getpid())) for name in entries)
        assert not any(len(name) == 64 for name in entries)  # full SHA-256 hex


def test_real_compile_meta_json_written(tmp_path: Path) -> None:
    import json as _json

    c = P4Compiler(cache_dir=tmp_path / "cache")
    result = c.compile(_FORWARD)
    meta_path = result.bmv2_json.parent / "meta.json"
    assert meta_path.is_file()
    meta: dict[str, Any] = _json.loads(meta_path.read_text())
    assert meta["hash"] == result.source_hash
    assert meta["arch"] == "v1model"
    assert meta["source_basename"] == "forward.p4"
    assert meta["compiler_version"] == result.compiler_version
    assert meta["timestamp"]
