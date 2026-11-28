"""Interactive prompt_toolkit shell for the p4net CLI."""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory

from p4net.cli.completers import build_network_completer
from p4net.cli.dispatcher import CommandDispatcher
from p4net.cli.exceptions import CLIExit, CLIUsageError, _CommandErrorOutput
from p4net.cli.formatting import red

if TYPE_CHECKING:
    from p4net.network import Network


_DEFAULT_HISTORY = Path.home() / ".p4net_history"


def _print_output(text: str) -> None:
    text = text.rstrip("\r\n")
    if text:
        print_formatted_text(ANSI(text), end="\n\n")


@contextmanager
def _cancel_command_on_sigint() -> Iterator[None]:
    """Use KeyboardInterrupt for command cancellation instead of network cleanup."""
    previous = (
        signal.getsignal(signal.SIGINT)
        if threading.current_thread() is threading.main_thread()
        else None
    )
    if previous is None:
        yield
        return

    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


class P4NetShell:
    """A thin prompt_toolkit REPL around a CommandDispatcher.

    Ctrl-C cancels the current input or command and re-prompts. Ctrl-D on an
    empty line exits cleanly. `exit` and `quit` raise `CLIExit`, which the loop
    catches to terminate.
    """

    def __init__(
        self,
        network: Network,
        *,
        history_file: Path | None = None,
        prompt: str = "p4net> ",
    ) -> None:
        self._network = network
        self._dispatcher = CommandDispatcher(
            network,
            color=True,
            on_progress=print_formatted_text,
            on_command_output=lambda text: print_formatted_text(ANSI(text), end=""),
        )
        self._history_file = Path(history_file) if history_file is not None else _DEFAULT_HISTORY
        self._prompt = prompt

    @property
    def dispatcher(self) -> CommandDispatcher:
        """The ``CommandDispatcher`` this shell delegates each line to."""
        return self._dispatcher

    def _print_error(self, detail: str) -> None:
        detail = detail.rstrip("\r\n")
        _print_output(red(f"Error: {detail}", color=self._dispatcher.color))

    def run(self) -> None:
        """Block, read lines, dispatch, until CLIExit or EOF."""
        history_path = self._history_file
        history_path.parent.mkdir(parents=True, exist_ok=True)
        completer = build_network_completer(self._dispatcher)
        session: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_path)),
            completer=completer,
        )
        while True:
            try:
                line = session.prompt(self._prompt)
            except KeyboardInterrupt:
                # Ctrl-C cancels the current input line.
                continue
            except EOFError:
                # Ctrl-D on empty line: exit cleanly.
                break
            try:
                with _cancel_command_on_sigint():
                    output = self._dispatcher.dispatch(line)
            except CLIExit:
                break
            except KeyboardInterrupt:
                # Ctrl-C while a command is running.
                _print_output("\x1b[33m^C cancelled\x1b[0m")
                continue
            except CLIUsageError as exc:
                self._print_error(str(exc))
            except Exception as exc:
                self._print_error(f"{type(exc).__name__}: {exc}")
            else:
                if isinstance(output, _CommandErrorOutput):
                    self._print_error(output.detail)
                else:
                    _print_output(output)
