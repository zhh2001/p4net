"""Coordinate the pipeline state of the two clients for a running switch."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from p4net.control.exceptions import P4RuntimeError
from p4net.control.p4info_index import P4InfoIndex


class _PipelineState:
    def __init__(self, index: P4InfoIndex | None = None) -> None:
        self.index = index
        self._lock = threading.Lock()
        self._active = 0
        self._changing = False

    @contextmanager
    def access(self, *, change: bool = False) -> Iterator[None]:
        # Avoid waiting across the sync and async clients. The sync caller
        # may be on the event loop needed to finish the other operation.
        with self._lock:
            if self._changing:
                raise P4RuntimeError("A pipeline change is in progress on this switch")
            if change and self._active:
                raise P4RuntimeError("Cannot change the pipeline while operations are using P4Info")
            self._active += 1
            self._changing = change
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
                if change:
                    self._changing = False

    def packet_in_index(self) -> P4InfoIndex | None:
        with self._lock:
            return None if self._changing else self.index
