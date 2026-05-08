"""Unit tests for `p4net.compiler.p4c`. All p4c invocations are mocked."""

from __future__ import annotations

import json
import subprocess
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
) -> Any:
    """A subprocess.run replacement that recognises both --version and compile."""
    state = {"version_calls": 0, "compile_calls": 0, "compile_argvs": []}

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        if "--version" in argv:
            state["version_calls"] = int(state["version_calls"]) + 1
            return _ok_proc(stdout=version_payload)
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

    # Step 2: a fresh compiler with a fresh patched subprocess that returns
    # only the version call. If anything tries to compile, that's a bug.
    version_only = MagicMock(side_effect=_make_smart_run())
    mocker.patch("p4net.compiler.p4c.subprocess.run", side_effect=version_only)
    c2 = P4Compiler(cache_dir=cache_dir)
    r2 = c2.compile(src)
    assert r2.cache_hit is True
    # subprocess.run was called only for --version, not for the compile.
    compile_calls = sum(
        1 for call in version_only.call_args_list if "--version" not in call.args[0]
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
