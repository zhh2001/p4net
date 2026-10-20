"""Wrapper around the `p4c` driver with content-addressed caching.

Every lookup runs ``p4c -E`` so the cache key includes resolved headers and
macro definitions. On a miss, ``p4c -e`` compiles that preprocessed snapshot
into BMv2 JSON and P4Info. Both invocations use the same target, architecture,
language version, and extra arguments.

Concurrent compiles stage their outputs in separate directories and publish
them with an atomic rename. If another writer has already published complete
artifacts, its entry is reused. No file locking is performed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from p4net.compiler.exceptions import CompileError, CompilerNotFoundError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompileResult:
    """Output of one :meth:`P4Compiler.compile` call.

    Attributes:
        source: The P4 source file that was compiled.
        arch: The target architecture (e.g. ``"v1model"``).
        bmv2_json: Path to the BMv2 JSON loaded by ``simple_switch_grpc``.
        p4info: Path to the P4Info text-protobuf describing the
            compiled program.
        compiler_version: The version string reported by ``p4c
            --version`` at compile time.
        source_hash: SHA-256 digest of the source path, source bytes,
            preprocessed input, architecture, compiler version, and arguments.
        cache_hit: ``True`` if backend compilation was skipped after preprocessing,
            ``False`` if the backend ran, including concurrent cache writes.
    """

    source: Path
    arch: str
    bmv2_json: Path
    p4info: Path
    compiler_version: str
    source_hash: str
    cache_hit: bool


def _frame(label: str, value: bytes) -> bytes:
    """Length-prefixed framing so distinct field values cannot collide."""
    return (
        b"|" + label.encode("utf-8") + b":" + str(len(value)).encode("ascii") + b":" + value + b"|"
    )


def _compute_hash(
    source_bytes: bytes,
    arch: str,
    compiler_version: str,
    extra_args: Sequence[str],
) -> str:
    hasher = hashlib.sha256()
    hasher.update(source_bytes)
    hasher.update(_frame("arch", arch.encode("utf-8")))
    hasher.update(_frame("version", compiler_version.encode("utf-8")))
    hasher.update(_frame("extra_args", json.dumps(list(extra_args)).encode("utf-8")))
    return hasher.hexdigest()


class P4Compiler:
    """Wrapper around the p4c driver with content-addressed caching."""

    DEFAULT_CACHE_DIR: ClassVar[Path] = Path.home() / ".cache" / "p4net" / "compiler"

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        p4c_binary: str = "p4c",
    ) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir is not None else self.DEFAULT_CACHE_DIR
        self._p4c_binary = p4c_binary
        self._version: str | None = None

    @property
    def cache_dir(self) -> Path:
        """Root of the content-addressed compile cache."""
        return self._cache_dir

    @property
    def p4c_binary(self) -> str:
        """Name or path of the ``p4c`` binary this compiler invokes."""
        return self._p4c_binary

    @property
    def version(self) -> str:
        """Return the p4c version string, calling the binary on first access.

        Raises:
            CompilerNotFoundError: the configured `p4c` binary is missing from PATH.
        """
        if self._version is not None:
            return self._version
        try:
            result = subprocess.run(
                [self._p4c_binary, "--version"],
                capture_output=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CompilerNotFoundError(
                f"p4c binary {self._p4c_binary!r} not found on PATH"
            ) from exc
        if result.returncode != 0:
            raise CompilerNotFoundError(
                f"p4c --version failed (rc={result.returncode}): "
                f"{result.stderr.decode('utf-8', errors='replace').strip()}"
            )
        # p4c prints the version banner on stdout in modern releases, but some
        # downstream packagers ship a build that uses stderr; combine both.
        text = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
        if not text:
            raise CompilerNotFoundError(
                f"p4c --version produced empty output for {self._p4c_binary!r}"
            )
        self._version = text
        return self._version

    def compile(
        self,
        source: Path,
        *,
        arch: str = "v1model",
        force: bool = False,
        extra_args: Sequence[str] = (),
    ) -> CompileResult:
        """Preprocess a .p4 source and reuse cached artifacts unless `force`.

        Preprocessing resolves includes and macros on every call. Cache misses
        compile the same preprocessed snapshot used to calculate the key.
        """
        source = Path(source)
        if not source.is_file():
            raise CompileError(source, 0, f"source file does not exist: {source}")

        source_bytes = source.read_bytes()
        version = self.version
        preprocessed = self._preprocess(source, arch=arch, extra_args=extra_args)
        cache_input = (
            _frame("source_path", os.fsencode(source.absolute()))
            + _frame("source", source_bytes)
            + _frame("preprocessed", preprocessed)
        )
        cache_key = _compute_hash(cache_input, arch, version, extra_args)
        final_dir = self._cache_dir / cache_key
        final_json = final_dir / "program.json"
        final_p4info = final_dir / "program.p4info.txtpb"

        if force and final_dir.exists():
            shutil.rmtree(final_dir, ignore_errors=False)
        elif self._cache_hit(final_dir, final_json, final_p4info):
            logger.debug("compiler cache hit: %s -> %s", source, cache_key)
            return CompileResult(
                source=source,
                arch=arch,
                bmv2_json=final_json,
                p4info=final_p4info,
                compiler_version=version,
                source_hash=cache_key,
                cache_hit=True,
            )

        logger.debug("compiler cache miss: %s -> %s; running p4c", source, cache_key)
        return self._real_compile(
            source=source,
            arch=arch,
            extra_args=tuple(extra_args),
            cache_key=cache_key,
            version=version,
            preprocessed=preprocessed,
        )

    # Internal helpers ---------------------------------------------------

    def _preprocess(self, source: Path, *, arch: str, extra_args: Sequence[str]) -> bytes:
        with tempfile.TemporaryDirectory(prefix="p4net-p4c-preprocess-") as tmp:
            output = Path(tmp) / f"{source.stem}.p4i"
            argv = [
                self._p4c_binary,
                "--target",
                "bmv2",
                "--arch",
                arch,
                "--std",
                "p4-16",
                "-E",
                "-o",
                tmp,
                *extra_args,
                str(source),
            ]
            logger.debug("preprocessing P4 source: %s", argv)
            try:
                result = subprocess.run(argv, capture_output=True, check=False)
            except FileNotFoundError as exc:
                raise CompilerNotFoundError(
                    f"p4c binary {self._p4c_binary!r} not found on PATH"
                ) from exc
            if result.returncode != 0:
                raise CompileError(
                    source,
                    result.returncode,
                    result.stderr.decode("utf-8", errors="replace"),
                )
            if not output.is_file() or output.stat().st_size == 0:
                raise CompileError(
                    source,
                    0,
                    "p4c reported success but preprocessed output is missing "
                    f"or empty: {output.name}",
                )
            return output.read_bytes()

    @staticmethod
    def _cache_hit(final_dir: Path, json_path: Path, p4info_path: Path) -> bool:
        if not final_dir.is_dir():
            return False
        if not json_path.is_file() or json_path.stat().st_size == 0:
            return False
        return p4info_path.is_file() and p4info_path.stat().st_size > 0

    def _real_compile(
        self,
        *,
        source: Path,
        arch: str,
        extra_args: tuple[str, ...],
        cache_key: str,
        version: str,
        preprocessed: bytes,
    ) -> CompileResult:
        with tempfile.TemporaryDirectory(prefix="p4net-p4c-") as tmp:
            tmp_path = Path(tmp)
            tmp_p4info = tmp_path / "program.p4info.txtpb"
            # With -e, the driver reads <out_dir>/<source_stem>.p4i.
            (tmp_path / f"{source.stem}.p4i").write_bytes(preprocessed)
            argv = [
                self._p4c_binary,
                "--target",
                "bmv2",
                "--arch",
                arch,
                "--std",
                "p4-16",
                "-e",
                "-o",
                str(tmp_path),
                "--p4runtime-files",
                str(tmp_p4info),
                *extra_args,
                str(source),
            ]
            logger.debug("running p4c: %s", argv)
            start = time.monotonic()
            try:
                result = subprocess.run(argv, capture_output=True, check=False)
            except FileNotFoundError as exc:
                raise CompilerNotFoundError(
                    f"p4c binary {self._p4c_binary!r} not found on PATH"
                ) from exc
            elapsed = time.monotonic() - start

            if result.returncode != 0:
                raise CompileError(
                    source,
                    result.returncode,
                    result.stderr.decode("utf-8", errors="replace"),
                )

            tmp_json = tmp_path / f"{source.stem}.json"
            if not tmp_json.is_file() or tmp_json.stat().st_size == 0:
                raise CompileError(
                    source,
                    0,
                    "p4c reported success but expected output files are missing "
                    f"(no non-empty {tmp_json.name} in {tmp_path})",
                )
            if not tmp_p4info.is_file() or tmp_p4info.stat().st_size == 0:
                raise CompileError(
                    source,
                    0,
                    "p4c reported success but expected output files are missing "
                    f"(no non-empty {tmp_p4info.name} in {tmp_path})",
                )

            os.makedirs(self._cache_dir, exist_ok=True)
            final_dir = self._cache_dir / cache_key
            with tempfile.TemporaryDirectory(
                prefix=f"{cache_key}.tmp-", dir=self._cache_dir
            ) as staging:
                staging_dir = Path(staging)
                shutil.move(str(tmp_json), str(staging_dir / "program.json"))
                shutil.move(str(tmp_p4info), str(staging_dir / "program.p4info.txtpb"))

                meta = {
                    "hash": cache_key,
                    "arch": arch,
                    "compiler_version": version,
                    "source_basename": source.name,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                with (staging_dir / "meta.json").open("w", encoding="utf-8") as fh:
                    json.dump(meta, fh, indent=2, sort_keys=True)

                try:
                    os.rename(str(staging_dir), str(final_dir))
                except OSError as exc:
                    if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not self._cache_hit(
                        final_dir, final_dir / "program.json", final_dir / "program.p4info.txtpb"
                    ):
                        raise
                    logger.debug(
                        "compiler cache race for %s: another writer populated %s",
                        source,
                        cache_key,
                    )

            logger.debug(
                "p4c compile of %s succeeded in %.3fs (cache key %s)",
                source,
                elapsed,
                cache_key,
            )

            return CompileResult(
                source=source,
                arch=arch,
                bmv2_json=final_dir / "program.json",
                p4info=final_dir / "program.p4info.txtpb",
                compiler_version=version,
                source_hash=cache_key,
                cache_hit=False,
            )
