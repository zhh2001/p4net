"""Interactive prompt_toolkit shell for the p4net CLI."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory

from p4net.cli.completers import build_network_completer
from p4net.cli.dispatcher import CommandDispatcher
from p4net.cli.exceptions import CLIExit, CLIUsageError

if TYPE_CHECKING:
    from p4net.network import Network


_DEFAULT_HISTORY = Path.home() / ".p4net_history"


class P4NetShell:
    """A thin prompt_toolkit REPL around a CommandDispatcher.

    Ctrl-C cancels the current input line and re-prompts; Ctrl-D on an
    empty line exits cleanly. `exit` and `quit` raise `CLIExit`, which
    the loop catches to terminate.
    """

    def __init__(
        self,
        network: Network,
        *,
        history_file: Path | None = None,
        prompt: str = "p4net> ",
    ) -> None:
        self._network = network
        self._dispatcher = CommandDispatcher(network, color=True)
        self._history_file = Path(history_file) if history_file is not None else _DEFAULT_HISTORY
        self._prompt = prompt

    @property
    def dispatcher(self) -> CommandDispatcher:
        """The :class:`CommandDispatcher` this shell delegates each line to."""
        return self._dispatcher

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
                output = self._dispatcher.dispatch(line)
            except CLIExit:
                break
            except KeyboardInterrupt:
                # Ctrl-C while a command is running.
                print_formatted_text(ANSI("\x1b[33m^C cancelled\x1b[0m"))
                continue
            except CLIUsageError as exc:
                print_formatted_text(ANSI(f"\x1b[31mError: {exc}\x1b[0m"))
                continue
            except Exception as exc:
                print_formatted_text(ANSI(f"\x1b[31mError: {type(exc).__name__}: {exc}\x1b[0m"))
                continue
            if output:
                print_formatted_text(ANSI(output))
