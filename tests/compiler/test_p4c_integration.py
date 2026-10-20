"""Integration tests that exercise a real `p4c` compile pipeline.

Gated by the `requires_p4c` marker (see `tests/conftest.py`); skipped by
default. Run with `pytest --run-p4c -m requires_p4c`.
"""

from __future__ import annotations

import json
import multiprocessing
import subprocess
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from p4net.compiler import CompileError, CompileResult, P4Compiler

pytestmark = pytest.mark.requires_p4c

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "p4"
_FORWARD = _FIXTURES / "forward.p4"
_SYNTAX_ERROR = _FIXTURES / "syntax_error.p4"


def _write_include_program(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "program.p4"
    header = directory / "settings.p4"
    source.write_text(
        '#include "settings.p4"\n'
        + _FORWARD.read_text().replace(
            "std.egress_spec = std.ingress_port;", "std.egress_spec = EGRESS_PORT;"
        )
    )
    header.write_text("#define EGRESS_PORT 1\n")
    return source, header


def _compiled_egress_port(path: Path) -> int:
    program = json.loads(path.read_text())
    for action in program["actions"]:
        for primitive in action["primitives"]:
            params = primitive["parameters"]
            if params[0].get("value") == ["standard_metadata", "egress_spec"]:
                return int(params[1]["value"], 16)
    raise AssertionError("compiled program has no egress port assignment")


@pytest.mark.parametrize("include_mode", ["quoted", "nested", "search-path", "macro"])
def test_include_change_invalidates_cache(tmp_path: Path, include_mode: str) -> None:
    source, header = _write_include_program(tmp_path / "source files")
    extra_args: tuple[str, ...] = ()
    if include_mode == "nested":
        nested = source.parent / "nested.p4"
        header.rename(nested)
        header.write_text('#include "nested.p4"\n')
        header = nested
    elif include_mode == "search-path":
        include_dir = tmp_path / "include"
        include_dir.mkdir()
        moved = include_dir / header.name
        header.rename(moved)
        header = moved
        source.write_text(source.read_text().replace('"settings.p4"', "<settings.p4>"))
        extra_args = ("-I", str(include_dir))
    elif include_mode == "macro":
        source.write_text(
            '#define SETTINGS_HEADER "settings.p4"\n'
            + source.read_text().replace('"settings.p4"', "SETTINGS_HEADER")
        )

    cache = tmp_path / "cache"
    first = P4Compiler(cache_dir=cache).compile(source, extra_args=extra_args)
    assert _compiled_egress_port(first.bmv2_json) == 1
    header.write_text("#define EGRESS_PORT 2\n")
    compiler = P4Compiler(cache_dir=cache)
    second = compiler.compile(source, extra_args=extra_args)
    assert not second.cache_hit
    assert second.source_hash != first.source_hash
    assert _compiled_egress_port(second.bmv2_json) == 2
    assert _compiled_egress_port(first.bmv2_json) == 1
    assert compiler.compile(source, extra_args=extra_args).cache_hit


def test_identical_sources_with_different_local_includes_do_not_share_cache(
    tmp_path: Path,
) -> None:
    source_a, _ = _write_include_program(tmp_path / "a")
    source_b, header_b = _write_include_program(tmp_path / "b")
    header_b.write_text("#define EGRESS_PORT 2\n")
    assert source_a.read_bytes() == source_b.read_bytes()
    compiler = P4Compiler(cache_dir=tmp_path / "cache")
    first = compiler.compile(source_a)
    second = compiler.compile(source_b)
    assert not second.cache_hit
    assert second.source_hash != first.source_hash
    assert _compiled_egress_port(second.bmv2_json) == 2


def test_deleted_include_cannot_return_cached_program(tmp_path: Path) -> None:
    source, header = _write_include_program(tmp_path)
    compiler = P4Compiler(cache_dir=tmp_path / "cache")
    compiler.compile(source)
    header.unlink()
    with pytest.raises(CompileError, match=r"settings\.p4"):
        compiler.compile(source)


def test_compile_uses_the_preprocessed_snapshot(tmp_path: Path, mocker: MockerFixture) -> None:
    source, header = _write_include_program(tmp_path)
    compiler = P4Compiler(cache_dir=tmp_path / "cache")
    real_run = subprocess.run

    def change_header_before_backend(argv: list[str], **kwargs: Any) -> Any:
        if "--p4runtime-files" in argv:
            header.write_text("#define EGRESS_PORT 2\n")
        return real_run(argv, **kwargs)

    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=change_header_before_backend)
    first = compiler.compile(source)
    assert _compiled_egress_port(first.bmv2_json) == 1
    second = compiler.compile(source)
    assert not second.cache_hit
    assert _compiled_egress_port(second.bmv2_json) == 2


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


def test_real_compile_cache_hit_skips_backend(tmp_path: Path, mocker: MockerFixture) -> None:
    c = P4Compiler(cache_dir=tmp_path / "cache")
    first = c.compile(_FORWARD)
    assert first.cache_hit is False

    # Cache hits still preprocess, but must not invoke the backend again.
    sentinel = MagicMock(side_effect=AssertionError("backend called on cache hit"))
    mocker.patch.object(c, "_real_compile", side_effect=sentinel)

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


def _compile_shared_source(source: Path, cache: Path) -> CompileResult:
    return P4Compiler(cache_dir=cache).compile(source)


@pytest.mark.parametrize("executor_kind", ["threads", "processes"])
def test_concurrent_compiles_publish_complete_cache(tmp_path: Path, executor_kind: str) -> None:
    source, _ = _write_include_program(tmp_path)
    cache = tmp_path / "cache"
    executor: ThreadPoolExecutor | ProcessPoolExecutor
    if executor_kind == "threads":
        executor = ThreadPoolExecutor(max_workers=4)
    else:
        executor = ProcessPoolExecutor(
            max_workers=4, mp_context=multiprocessing.get_context("spawn")
        )
    with executor:
        futures = [executor.submit(_compile_shared_source, source, cache) for _ in range(4)]
        results = [future.result(timeout=30) for future in futures]

    assert len({result.source_hash for result in results}) == 1
    for result in results:
        assert _compiled_egress_port(result.bmv2_json) == 1
        assert result.p4info.stat().st_size > 0
        metadata = json.loads((result.bmv2_json.parent / "meta.json").read_text())
        assert metadata["hash"] == result.source_hash
    assert list(cache.iterdir()) == [results[0].bmv2_json.parent]
    assert P4Compiler(cache_dir=cache).compile(source).cache_hit


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
        assert list(cache_dir.iterdir()) == []


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
