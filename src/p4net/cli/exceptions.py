"""Exception hierarchy for the interactive CLI."""

from __future__ import annotations

from p4net.runtime.exceptions import P4NetError


class CLIError(P4NetError):
    """Base class for command-line shell failures."""


class CLIUsageError(CLIError):
    """Raised on bad command syntax. The shell renders these as red errors."""


class CLIExit(CLIError):
    """Raised by `exit` / `quit` to ask the shell to terminate cleanly."""


class _CommandErrorOutput(str):
    """Error text returned by a command handler for the shell to style."""

    @classmethod
    def from_exception(cls, exc: Exception) -> _CommandErrorOutput:
        return cls(f"error: {type(exc).__name__}: {exc}")

    @property
    def detail(self) -> str:
        return self.removeprefix("error: ")
