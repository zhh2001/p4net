"""prompt_toolkit completer for the p4net shell."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias

from prompt_toolkit.completion import NestedCompleter

if TYPE_CHECKING:
    from p4net.cli.dispatcher import CommandDispatcher

_TopicTree: TypeAlias = dict[str, "_TopicTree"]


def build_network_completer(dispatcher: CommandDispatcher) -> NestedCompleter:
    """Build a NestedCompleter that knows about commands, hosts, and switches."""
    nested: dict[str, NestedCompleter | dict[str, None] | None] = {}
    for cmd in dispatcher.command_names:
        nested[cmd] = None
    # `topology` has one sub-verb so far: `graph`.
    if "topology" in nested:
        nested["topology"] = {"graph": None}
    # Host commands: <host> [ping|ping6|cmd|ifconfig|xterm]
    host_subs = _host_subcommands_for(dispatcher)
    for name in dispatcher.host_names:
        nested[name] = dict.fromkeys(host_subs, None)
    # Switch commands.
    for name in dispatcher.switch_names:
        nested[name] = dict.fromkeys(_switch_subcommands_for(dispatcher), None)
    nested["help"] = _help_completer(dispatcher)
    return NestedCompleter.from_nested_dict(nested)


def _help_completer(dispatcher: CommandDispatcher) -> NestedCompleter:
    topics: _TopicTree = {}
    for topic in dispatcher._help_topics:
        branch = topics
        for word in topic.split():
            branch = branch.setdefault(word, {})
    for name in [*dispatcher.host_names, *dispatcher.switch_names]:
        prefix = dispatcher._node_help_prefix(name)
        if prefix is not None and prefix in topics:
            topics.setdefault(name, topics[prefix])
    return NestedCompleter.from_nested_dict(topics)


def _switch_subcommands_for(dispatcher: CommandDispatcher) -> list[str]:
    """Read the dispatcher's switch handler keys at completer-build time.

    This helper keeps the completer in sync without requiring two import paths.
    """
    # `_switch_handlers` is private but stable; the alternative is duplicating
    # the verb list, which would drift.
    handlers = getattr(dispatcher, "_switch_handlers", {})
    return sorted(handlers.keys())


def _host_subcommands_for(dispatcher: CommandDispatcher) -> list[str]:
    """Same idea for host verbs."""
    handlers = getattr(dispatcher, "_host_handlers", {})
    return sorted(handlers.keys())
