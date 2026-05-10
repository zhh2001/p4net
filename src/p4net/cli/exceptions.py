"""Exception hierarchy for the interactive CLI."""

from __future__ import annotations

from p4net.runtime.exceptions import P4NetError


class CLIError(P4NetError):
    """Base class for command-line shell failures."""


class CLIUsageError(CLIError):
    """Raised on bad command syntax. The shell renders these as red errors."""


class CLIExit(CLIError):
    """Raised by `exit` / `quit` to ask the shell to terminate cleanly."""
