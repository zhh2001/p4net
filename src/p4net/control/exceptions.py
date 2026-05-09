"""Exception hierarchy for the P4Runtime control client."""

from __future__ import annotations

from p4net.runtime.exceptions import P4NetError


class P4RuntimeError(P4NetError):
    """Base class for P4Runtime client failures."""


class ConnectionError(P4RuntimeError):
    """Failure to open the gRPC channel or complete master arbitration."""


class NotPrimaryError(P4RuntimeError):
    """The local client is not the primary controller for this device."""


class PipelineError(P4RuntimeError):
    """SetForwardingPipelineConfig was rejected by the switch."""


class NoSuchTableError(P4RuntimeError):
    """Referenced table is not present in the current P4Info."""


class NoSuchActionError(P4RuntimeError):
    """Referenced action is not present in the current P4Info."""


class NoSuchFieldError(P4RuntimeError):
    """Referenced match field or action parameter is not present."""


class EncodingError(P4RuntimeError):
    """A value could not be encoded for the declared field bitwidth or match type."""


class DuplicateEntryError(P4RuntimeError):
    """Insert failed because the entry already exists."""


class EntryNotFoundError(P4RuntimeError):
    """Modify or delete failed because the entry does not exist."""
