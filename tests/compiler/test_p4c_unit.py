"""Unit tests for `p4net.compiler.p4c`. All p4c invocations are mocked."""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from p4net.compiler import CompileError, CompilerNotFoundError, P4Compiler
from p4net.compiler.p4c import _compute_hash

# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_constructor_defaults() -> None:
    c = P4Compiler()
    assert c.cache_dir == P4Compiler.DEFAULT_CACHE_DIR
    assert c.p4c_binary == "p4c"


def test_constructor_custom(tmp_path: Path) -> None:
    c = P4Compiler(cache_dir=tmp_path / "cache", p4c_binary="my-p4c")
    assert c.cache_dir == tmp_path / "cache"
    assert c.p4c_binary == "my-p4c"


# ---------------------------------------------------------------------------
# version property
# ---------------------------------------------------------------------------


def _ok_proc(stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


def _bad_proc(rc: int, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=b"", stderr=stderr)


def test_version_reads_from_subprocess(mocker: MockerFixture, tmp_path: Path) -> None:
    fake_run = mocker.patch(
        "p4net.compiler.p4c.subprocess.run",
        return_value=_ok_proc(stdout=b"p4c 1.2.4 (SHA: abc123)\n"),
    )
    c = P4Compiler(cache_dir=tmp_path / "cache")
    assert c.version == "p4c 1.2.4 (SHA: abc123)"
    fake_run.assert_called_once()
    argv = fake_run.call_args.args[0]
    assert argv == ["p4c", "--version"]


def test_version_falls_back_to_stderr(mocker: MockerFixture, tmp_path: Path) -> None:
    mocker.patch(
        "p4net.compiler.p4c.subprocess.run",
        return_value=_ok_proc(stderr=b"p4c version 1.2.4\n"),
    )
    c = P4Compiler(cache_dir=tmp_path / "cache")
    assert c.version == "p4c version 1.2.4"


def test_version_is_cached_on_instance(mocker: MockerFixture, tmp_path: Path) -> None:
    fake_run = mocker.patch(
        "p4net.compiler.p4c.subprocess.run",
        return_value=_ok_proc(stdout=b"p4c 1.2.4\n"),
    )
    c = P4Compiler(cache_dir=tmp_path / "cache")
    _ = c.version
    _ = c.version
    _ = c.version
    fake_run.assert_called_once()


def test_version_raises_when_binary_missing(mocker: MockerFixture, tmp_path: Path) -> None:
    mocker.patch(
        "p4net.compiler.p4c.subprocess.run",
        side_effect=FileNotFoundError("[Errno 2] No such file or directory: 'p4c'"),
    )
    c = P4Compiler(cache_dir=tmp_path / "cache", p4c_binary="not-a-real-binary")
    with pytest.raises(CompilerNotFoundError, match="not-a-real-binary"):
        _ = c.version


def test_version_raises_when_subprocess_returns_nonzero(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    mocker.patch(
        "p4net.compiler.p4c.subprocess.run",
        return_value=_bad_proc(2, stderr=b"bad call"),
    )
    c = P4Compiler(cache_dir=tmp_path / "cache")
    with pytest.raises(CompilerNotFoundError, match="rc=2"):
        _ = c.version


def test_version_raises_when_output_empty(mocker: MockerFixture, tmp_path: Path) -> None:
    mocker.patch(
        "p4net.compiler.p4c.subprocess.run",
        return_value=_ok_proc(),
    )
    c = P4Compiler(cache_dir=tmp_path / "cache")
    with pytest.raises(CompilerNotFoundError, match="empty output"):
        _ = c.version


# ---------------------------------------------------------------------------
# Cache-key determinism
# ---------------------------------------------------------------------------


def test_cache_key_deterministic_for_same_inputs() -> None:
    a = _compute_hash(b"src bytes", "v1model", "p4c 1.2.4", ())
    b = _compute_hash(b"src bytes", "v1model", "p4c 1.2.4", ())
    assert a == b


def test_cache_key_changes_with_source() -> None:
    a = _compute_hash(b"alpha", "v1model", "p4c 1.2.4", ())
    b = _compute_hash(b"beta", "v1model", "p4c 1.2.4", ())
    assert a != b


def test_cache_key_changes_with_arch() -> None:
    a = _compute_hash(b"src", "v1model", "p4c 1.2.4", ())
    b = _compute_hash(b"src", "psa", "p4c 1.2.4", ())
    assert a != b


def test_cache_key_changes_with_version() -> None:
    a = _compute_hash(b"src", "v1model", "p4c 1.2.4", ())
    b = _compute_hash(b"src", "v1model", "p4c 1.2.5", ())
    assert a != b


def test_cache_key_changes_with_extra_args() -> None:
    a = _compute_hash(b"src", "v1model", "p4c 1.2.4", ())
    b = _compute_hash(b"src", "v1model", "p4c 1.2.4", ("--Wdisable=unused",))
    assert a != b


def test_cache_key_extra_args_order_matters() -> None:
    a = _compute_hash(b"src", "v1model", "p4c 1.2.4", ("--a", "--b"))
    b = _compute_hash(b"src", "v1model", "p4c 1.2.4", ("--b", "--a"))
    assert a != b


def test_cache_key_distinguishes_arch_and_version_swap() -> None:
    """Framing must prevent collision when 'arch' and 'version' values are swapped."""
    a = _compute_hash(b"src", "alpha", "beta", ())
    b = _compute_hash(b"src", "beta", "alpha", ())
    assert a != b


# ---------------------------------------------------------------------------
# Helpers for compile() tests
# ---------------------------------------------------------------------------


def _write_p4(path: Path, content: bytes = b"// fake p4 source\n") -> None:
    path.write_bytes(content)


def _make_smart_run(
    *,
    bmv2_json_payload: bytes = b'{"fake":"json"}',
    p4info_payload: bytes = b"fake p4info",
    version_payload: bytes = b"p4c 1.2.4 (test)\n",
    rc: int = 0,
    stderr: bytes = b"",
    preprocessed_payload: bytes | None = None,
) -> Any:
    """A subprocess.run replacement for version, preprocessing, and compilation."""
    state = {
        "version_calls": 0,
        "preprocess_calls": 0,
        "preprocess_argvs": [],
        "compile_calls": 0,
        "compile_argvs": [],
    }

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        if "--version" in argv:
            state["version_calls"] = int(state["version_calls"]) + 1
            return _ok_proc(stdout=version_payload)
        if "-E" in argv:
            state["preprocess_calls"] = int(state["preprocess_calls"]) + 1
            argvs_list = state["preprocess_argvs"]
            assert isinstance(argvs_list, list)
            argvs_list.append(list(argv))
            out_dir = Path(argv[argv.index("-o") + 1])
            source = Path(argv[-1])
            payload = source.read_bytes() if preprocessed_payload is None else preprocessed_payload
            (out_dir / f"{source.stem}.p4i").write_bytes(payload)
            return _ok_proc()
        # Compile path.
        state["compile_calls"] = int(state["compile_calls"]) + 1
        argvs_list = state["compile_argvs"]
        assert isinstance(argvs_list, list)
        argvs_list.append(list(argv))
        if rc != 0:
            return _bad_proc(rc, stderr=stderr)
        # p4c writes <source_stem>.json into the -o dir, and p4info to the requested path.
        out_dir = Path(argv[argv.index("-o") + 1])
        p4info_path = Path(argv[argv.index("--p4runtime-files") + 1])
        source = Path(argv[-1])
        (out_dir / f"{source.stem}.json").write_bytes(bmv2_json_payload)
        p4info_path.write_bytes(p4info_payload)
        return _ok_proc()

    fake_run.state = state  # type: ignore[attr-defined]
    return fake_run


# ---------------------------------------------------------------------------
# Cache hit / miss / force
# ---------------------------------------------------------------------------


def test_cache_hit_short_circuits(mocker: MockerFixture, tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    src = tmp_path / "prog.p4"
    _write_p4(src, b"sentinel-source")

    # Step 1: populate the cache via a real (mocked) compile.
    fake = _make_smart_run()
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=fake)
    c1 = P4Compiler(cache_dir=cache_dir)
    r1 = c1.compile(src)
    assert r1.cache_hit is False
    assert fake.state["compile_calls"] == 1

    # A fresh compiler must recheck preprocessed input without recompiling.
    cached_run = MagicMock(side_effect=_make_smart_run())
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=cached_run)
    c2 = P4Compiler(cache_dir=cache_dir)
    r2 = c2.compile(src)
    assert r2.cache_hit is True
    compile_calls = sum(
        1 for call in cached_run.call_args_list if "--p4runtime-files" in call.args[0]
    )
    assert compile_calls == 0


def test_cache_miss_installs_outputs(mocker: MockerFixture, tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    src = tmp_path / "prog.p4"
    _write_p4(src)

    fake = _make_smart_run(
        bmv2_json_payload=b'{"answer":42}',
        p4info_payload=b"action_specs { name: 'a' }",
    )
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=fake)

    c = P4Compiler(cache_dir=cache_dir)
    r = c.compile(src)
    assert r.cache_hit is False
    assert r.bmv2_json.is_file()
    assert r.p4info.is_file()
    assert r.bmv2_json.read_bytes() == b'{"answer":42}'
    assert r.p4info.read_bytes() == b"action_specs { name: 'a' }"

    # Cache directory layout
    assert r.bmv2_json.parent == cache_dir / r.source_hash
    assert (cache_dir / r.source_hash / "meta.json").is_file()
    meta = json.loads((cache_dir / r.source_hash / "meta.json").read_text())
    assert meta["hash"] == r.source_hash
    assert meta["arch"] == "v1model"
    assert meta["source_basename"] == "prog.p4"


def test_compile_argv_includes_extra_args(mocker: MockerFixture, tmp_path: Path) -> None:
    src = tmp_path / "prog.p4"
    _write_p4(src)
    fake = _make_smart_run()
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=fake)
    c = P4Compiler(cache_dir=tmp_path / "cache")
    c.compile(src, extra_args=["--Wdisable=unused"])
    argv = fake.state["compile_argvs"][0]
    assert "--Wdisable=unused" in argv
    # It is positioned before the source path.
    assert argv.index("--Wdisable=unused") < argv.index(str(src))
    assert "--Wdisable=unused" in fake.state["preprocess_argvs"][0]
    assert "-e" in argv


def test_force_bypasses_cache_hit(mocker: MockerFixture, tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    src = tmp_path / "prog.p4"
    _write_p4(src)

    fake = _make_smart_run()
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=fake)
    c = P4Compiler(cache_dir=cache_dir)
    r1 = c.compile(src)
    r2 = c.compile(src)
    assert r1.cache_hit is False
    assert r2.cache_hit is True
    assert fake.state["compile_calls"] == 1

    # force=True should re-run.
    r3 = c.compile(src, force=True)
    assert r3.cache_hit is False
    assert fake.state["compile_calls"] == 2


def test_concurrent_threads_keep_their_staged_artifacts(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    source = tmp_path / "program.p4"
    _write_p4(source)
    cache = tmp_path / "cache"
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=_make_smart_run())
    first_staged = threading.Event()
    second_finished = threading.Event()
    main_thread = threading.get_ident()
    real_move = shutil.move

    def pause_first_writer(src: str, dst: str) -> str:
        result = real_move(src, dst)
        if Path(dst).name == "program.json" and threading.get_ident() != main_thread:
            first_staged.set()
            assert second_finished.wait(10), "second writer did not finish"
        return result

    mocker.patch("p4net.compiler.p4c.shutil.move", side_effect=pause_first_writer)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(P4Compiler(cache_dir=cache).compile, source)
        try:
            assert first_staged.wait(10), "first writer did not stage its JSON"
            second = P4Compiler(cache_dir=cache).compile(source)
        finally:
            second_finished.set()
        result = first.result(timeout=10)

    assert result.source_hash == second.source_hash
    assert result.bmv2_json.read_bytes() == b'{"fake":"json"}'
    assert result.p4info.read_bytes() == b"fake p4info"
    assert list(cache.iterdir()) == [result.bmv2_json.parent]
    assert P4Compiler(cache_dir=cache).compile(source).cache_hit


@pytest.mark.parametrize("error_number", [errno.EEXIST, errno.ENOTEMPTY])
def test_publish_race_reuses_complete_entry(
    mocker: MockerFixture, tmp_path: Path, error_number: int
) -> None:
    source = tmp_path / "program.p4"
    _write_p4(source)
    cache = tmp_path / "cache"
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=_make_smart_run())
    real_rename = os.rename

    def another_writer_publishes(src: str, dst: str) -> None:
        target = Path(dst)
        if target.parent == cache:
            target.mkdir()
            (target / "program.json").write_bytes(b'{"winner":true}')
            (target / "program.p4info.txtpb").write_bytes(b"winner p4info")
            (target / "meta.json").write_text("{}")
            raise OSError(error_number, os.strerror(error_number), dst)
        real_rename(src, dst)

    mocker.patch("p4net.compiler.p4c.os.rename", side_effect=another_writer_publishes)
    result = P4Compiler(cache_dir=cache).compile(source)
    assert result.bmv2_json.read_bytes() == b'{"winner":true}'
    assert result.p4info.read_bytes() == b"winner p4info"
    assert list(cache.iterdir()) == [result.bmv2_json.parent]
    assert P4Compiler(cache_dir=cache).compile(source).cache_hit


@pytest.mark.parametrize("failure_at", ["move", "rename"])
def test_failed_cache_write_cleans_up_staging(
    mocker: MockerFixture, tmp_path: Path, failure_at: str
) -> None:
    source = tmp_path / "program.p4"
    _write_p4(source)
    cache = tmp_path / "cache"
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=_make_smart_run())
    error = OSError(errno.ENOSPC, "disk full")
    real_move = shutil.move
    real_rename = os.rename

    def fail_move(src: str, dst: str) -> str:
        if Path(dst).name == "program.p4info.txtpb":
            raise error
        return real_move(src, dst)

    def fail_rename(src: str, dst: str) -> None:
        if Path(dst).parent == cache:
            raise error
        real_rename(src, dst)

    if failure_at == "move":
        mocker.patch("p4net.compiler.p4c.shutil.move", side_effect=fail_move)
    else:
        mocker.patch("p4net.compiler.p4c.os.rename", side_effect=fail_rename)
    with pytest.raises(OSError) as caught:
        P4Compiler(cache_dir=cache).compile(source)
    assert caught.value is error
    assert list(cache.iterdir()) == []


def test_publish_race_rejects_incomplete_entry(mocker: MockerFixture, tmp_path: Path) -> None:
    source = tmp_path / "program.p4"
    _write_p4(source)
    cache = tmp_path / "cache"
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=_make_smart_run())
    real_rename = os.rename
    targets = []

    def incomplete_writer(src: str, dst: str) -> None:
        target = Path(dst)
        if target.parent == cache:
            target.mkdir()
            (target / "program.json").write_bytes(b"{}")
            targets.append(target)
        real_rename(src, dst)

    mocker.patch("p4net.compiler.p4c.os.rename", side_effect=incomplete_writer)
    with pytest.raises(OSError) as caught:
        P4Compiler(cache_dir=cache).compile(source)
    assert caught.value.errno in (errno.EEXIST, errno.ENOTEMPTY)
    assert len(targets) == 1
    assert list(cache.iterdir()) == targets


def test_changed_preprocessed_input_invalidates_cache(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    source = tmp_path / "program.p4"
    _write_p4(source, b'#include "settings.p4"\n')
    cache = tmp_path / "cache"
    mocker.patch(
        "p4net.compiler.p4c.subprocess.run",
        side_effect=_make_smart_run(preprocessed_payload=b"const bit<9> PORT = 1;"),
    )
    first = P4Compiler(cache_dir=cache).compile(source)
    mocker.patch(
        "p4net.compiler.p4c.subprocess.run",
        side_effect=_make_smart_run(preprocessed_payload=b"const bit<9> PORT = 2;"),
    )
    compiler = P4Compiler(cache_dir=cache)
    second = compiler.compile(source)
    assert not second.cache_hit
    assert second.source_hash != first.source_hash
    assert compiler.compile(source).cache_hit


def test_relative_source_paths_in_different_directories_have_separate_cache_entries(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compiler = P4Compiler(cache_dir=tmp_path / "cache")
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=_make_smart_run())
    directories = (tmp_path / "a", tmp_path / "b")
    results = []
    for directory in directories:
        directory.mkdir()
        _write_p4(directory / "program.p4")
        monkeypatch.chdir(directory)
        results.append(compiler.compile(Path("program.p4")))
    assert results[0].source_hash != results[1].source_hash
    assert not results[1].cache_hit


def test_preprocessing_failure_does_not_return_cached_artifacts(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    source = tmp_path / "program.p4"
    _write_p4(source)
    compiler = P4Compiler(cache_dir=tmp_path / "cache")
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=_make_smart_run())
    first = compiler.compile(source)
    mocker.patch(
        "p4net.compiler.p4c.subprocess.run",
        return_value=_bad_proc(1, stderr=b"settings.p4: No such file or directory"),
    )
    with pytest.raises(CompileError, match="No such file"):
        compiler.compile(source)
    assert first.bmv2_json.is_file()
    assert first.p4info.is_file()


@pytest.mark.parametrize("empty_file", [False, True])
def test_preprocessing_requires_output(
    mocker: MockerFixture, tmp_path: Path, empty_file: bool
) -> None:
    source = tmp_path / "program.p4"
    _write_p4(source)

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "--version" in argv:
            return _ok_proc(stdout=b"p4c test")
        if empty_file:
            out_dir = Path(argv[argv.index("-o") + 1])
            (out_dir / "program.p4i").touch()
        return _ok_proc()

    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=run)
    with pytest.raises(CompileError, match="preprocessed output is missing or empty"):
        P4Compiler(cache_dir=tmp_path / "cache").compile(source)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_compile_raises_when_source_missing(tmp_path: Path) -> None:
    c = P4Compiler(cache_dir=tmp_path / "cache")
    with pytest.raises(CompileError, match="does not exist"):
        c.compile(tmp_path / "no-such.p4")


def test_compile_raises_when_p4c_returns_nonzero(mocker: MockerFixture, tmp_path: Path) -> None:
    src = tmp_path / "prog.p4"
    _write_p4(src)
    fake = _make_smart_run(rc=1, stderr=b"syntax error: line 7")
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=fake)
    c = P4Compiler(cache_dir=tmp_path / "cache")
    with pytest.raises(CompileError) as info:
        c.compile(src)
    assert info.value.returncode == 1
    assert "syntax error" in str(info.value)
    # Cache must not have been populated.
    cache_entries = list((tmp_path / "cache").glob("*"))
    assert all(p.suffix == ".tmp" or not p.is_dir() for p in cache_entries) or cache_entries == []


def test_compile_raises_when_outputs_missing(mocker: MockerFixture, tmp_path: Path) -> None:
    src = tmp_path / "prog.p4"
    _write_p4(src)

    def silent_success(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        if "--version" in argv:
            return _ok_proc(stdout=b"p4c 1.2.4")
        if "-E" in argv:
            out_dir = Path(argv[argv.index("-o") + 1])
            (out_dir / f"{src.stem}.p4i").write_bytes(src.read_bytes())
        return _ok_proc()  # no files written

    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=silent_success)
    c = P4Compiler(cache_dir=tmp_path / "cache")
    with pytest.raises(CompileError, match="expected output files are missing"):
        c.compile(src)


def test_compile_raises_when_p4info_missing(mocker: MockerFixture, tmp_path: Path) -> None:
    src = tmp_path / "prog.p4"
    _write_p4(src)

    def half_success(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        if "--version" in argv:
            return _ok_proc(stdout=b"p4c 1.2.4")
        out_dir = Path(argv[argv.index("-o") + 1])
        source = Path(argv[-1])
        if "-E" in argv:
            (out_dir / f"{source.stem}.p4i").write_bytes(source.read_bytes())
            return _ok_proc()
        (out_dir / f"{source.stem}.json").write_bytes(b"{}")
        # Skip the p4info file.
        return _ok_proc()

    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=half_success)
    c = P4Compiler(cache_dir=tmp_path / "cache")
    with pytest.raises(CompileError, match="expected output files are missing"):
        c.compile(src)


def test_compile_translates_filenotfound_to_compiler_not_found(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    src = tmp_path / "prog.p4"
    _write_p4(src)

    def raise_fnf(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        if "--version" in argv:
            return _ok_proc(stdout=b"p4c 1.2.4")
        raise FileNotFoundError("compiler vanished mid-flight")

    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=raise_fnf)
    c = P4Compiler(cache_dir=tmp_path / "cache")
    with pytest.raises(CompilerNotFoundError):
        c.compile(src)


# ---------------------------------------------------------------------------
# CompileError __str__
# ---------------------------------------------------------------------------


def test_compile_error_str_truncates_long_stderr(tmp_path: Path) -> None:
    big = "x" * 10_000
    err = CompileError(tmp_path / "prog.p4", 1, big)
    text = str(err)
    assert "stderr truncated" in text
    assert len(text) < 6000  # original 10k stderr is truncated


def test_compile_error_str_keeps_short_stderr(tmp_path: Path) -> None:
    err = CompileError(tmp_path / "prog.p4", 7, "tiny error")
    text = str(err)
    assert "tiny error" in text
    assert "return code: 7" in text
