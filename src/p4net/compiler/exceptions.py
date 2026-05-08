"""Exception hierarchy for the p4net compiler layer."""

from __future__ import annotations

from pathlib import Path

from p4net.runtime.exceptions import P4NetError

_STDERR_TRUNCATE_BYTES = 4096


class CompilerNotFoundError(P4NetError):
    """Raised when the configured p4c binary is not on PATH."""


class CompileError(P4NetError):
    """Raised when p4c rejects a source file.

    Attributes:
        source: The .p4 source path that failed.
        returncode: p4c's exit code.
        stderr: Captured stderr text.
    """

    def __init__(self, source: Path, returncode: int, stderr: str) -> None:
        self.source = source
        self.returncode = returncode
        self.stderr = stderr
        if len(stderr) > _STDERR_TRUNCATE_BYTES:
            shown_stderr = stderr[:_STDERR_TRUNCATE_BYTES] + "\n... [stderr truncated]"
        else:
            shown_stderr = stderr
        message = (
            f"p4c failed to compile {source}\n"
            f"  return code: {returncode}\n"
            f"  stderr:\n{shown_stderr}"
        )
        super().__init__(message)
