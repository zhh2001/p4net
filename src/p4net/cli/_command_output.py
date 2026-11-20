"""Stream a host command's captured output to the interactive shell."""

from __future__ import annotations

import codecs
import os
import selectors
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from p4net.runtime import NSProcess


def stream_command_output(
    process: NSProcess, argv: Sequence[str], timeout: int, write: Callable[[str], None]
) -> int:
    stdout, stderr = process.stdout, process.stderr
    assert stdout is not None and stderr is not None
    deadline = time.monotonic() + timeout
    decoders = {
        is_stderr: codecs.getincrementaldecoder("utf-8")(errors="replace")
        for is_stderr in (False, True)
    }
    previous_stream: bool | None = None
    starts_line = True

    def display(text: str, is_stderr: bool) -> None:
        nonlocal previous_stream, starts_line
        if not text:
            return
        if previous_stream != is_stderr and not starts_line:
            write("\n")
            starts_line = True
        previous_stream = is_stderr
        if is_stderr:
            lines = text.splitlines(keepends=True)
            text = "".join(
                f"[stderr] {line}" if i or starts_line else line for i, line in enumerate(lines)
            )
        starts_line = text.endswith("\n")
        write(text)

    with selectors.DefaultSelector() as selector:
        selector.register(stdout, selectors.EVENT_READ, False)
        selector.register(stderr, selectors.EVENT_READ, True)
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not (ready := selector.select(remaining)):
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in ready:
                    data = os.read(key.fd, 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                    display(decoders[key.data].decode(data, final=not data), key.data)
            result = process.wait(timeout=max(0, deadline - time.monotonic()))
        finally:
            if not starts_line:
                write("\n")
    if result == 0 and previous_stream is not None:
        write("\n")
    return result
