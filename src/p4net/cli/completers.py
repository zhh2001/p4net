"""prompt_toolkit completer for the p4net shell."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit.completion import NestedCompleter

if TYPE_CHECKING:
    from p4net.cli.dispatcher import CommandDispatcher


def build_network_completer(dispatcher: CommandDispatcher) -> NestedCompleter:
    """Build a NestedCompleter that knows about commands, hosts, and switches."""
    nested: dict[str, dict[str, None] | None] = {}
    for cmd in dispatcher.command_names:
        nested[cmd] = None
    # Host commands: <host> [ping|ping6|cmd|ifconfig]
    host_subs = _host_subcommands_for(dispatcher)
    for name in dispatcher.host_names:
        nested[name] = dict.fromkeys(host_subs, None)
    # Switch commands. The full set lands in commit 3; here we wire enough
    # so the completer at least offers the verbs it knows about now.
    for name in dispatcher.switch_names:
        nested[name] = dict.fromkeys(_switch_subcommands_for(dispatcher), None)
    return NestedCompleter.from_nested_dict(nested)


def _switch_subcommands_for(dispatcher: CommandDispatcher) -> list[str]:
    """Read the dispatcher's switch handler keys at completer-build time.

    Phase-2 ships an empty handler map; phase-3 fills it in. This helper
    keeps the completer in sync without requiring two import paths.
    """
    # `_switch_handlers` is private but stable; the alternative is duplicating
    # the verb list, which would drift.
    handlers = getattr(dispatcher, "_switch_handlers", {})
    return sorted(handlers.keys())


def _host_subcommands_for(dispatcher: CommandDispatcher) -> list[str]:
    """Same idea for host verbs."""
    handlers = getattr(dispatcher, "_host_handlers", {})
    return sorted(handlers.keys())
