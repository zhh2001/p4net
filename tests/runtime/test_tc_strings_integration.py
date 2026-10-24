"""Check tc string validation against real root and namespace qdiscs."""

from __future__ import annotations

import json
import math
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

import p4net.runtime.tc as tc
from p4net.runtime import NetworkNamespace, TcError, VethPair, apply_netem, clear_qdisc

pytestmark = pytest.mark.integration

_INVALID_UNIT_FORMATS = [
    (field, "1" + separator + ("mbit" if field == "rate" else "ms"))
    for field in ("rate", "delay", "jitter")
    for separator in (" ", "\t", "\n", "\r", "\v", "\f", "\u00a0", "\u2003", "\u2028")
] + [
    ("rate", "1b\u0131t"),
    ("rate", "1b\u0130t"),
    ("delay", "1m\u017f"),
    ("delay", "1u\u017f"),
    ("delay", "1\u017f"),
    ("jitter", "1m\u017f"),
    ("jitter", "1u\u017f"),
    ("jitter", "1\u017f"),
]


@contextmanager
def _interface(in_namespace: bool) -> Iterator[tuple[NetworkNamespace | None, str]]:
    suffix = uuid.uuid4().hex[:8]
    with NetworkNamespace(f"ns{suffix}") as namespace:
        pair = VethPair(f"vA_{suffix}", f"vB_{suffix}", namespace_a=namespace)
        try:
            pair.create()
            yield (namespace, pair.name_a) if in_namespace else (None, pair.name_b)
        finally:
            pair.destroy()


def _state(namespace: NetworkNamespace | None, interface: str) -> dict[str, Any] | None:
    command = ["tc", "-j", "qdisc", "show", "dev", interface]
    output = (
        subprocess.check_output(command)
        if namespace is None
        else namespace.exec(command, capture_output=True).stdout
    )
    entries = [entry for entry in json.loads(output) if entry["kind"] == "netem"]
    if not entries:
        return None
    assert len(entries) == 1
    return {key: entries[0][key] for key in ("handle", "options")}


@pytest.mark.parametrize("in_namespace", [False, True])
@pytest.mark.parametrize("ending", ["\n", "\n\n", "\r\n"])
@pytest.mark.parametrize("field", ["rate", "delay", "jitter"])
def test_trailing_newline_preserves_existing_qdisc(
    in_namespace: bool, ending: str, field: str
) -> None:
    with _interface(in_namespace) as (namespace, interface):
        apply_netem(namespace, interface, rate="8mbit", delay="2ms", jitter="0.5ms")
        before = _state(namespace, interface)
        assert before is not None
        values = {field: ("10mbit" if field == "rate" else "1ms") + ending}
        if field == "jitter":
            values["delay"] = "10ms"
        with patch.object(tc, "_run", wraps=tc._run) as run:
            with pytest.raises(TcError, match=r"invalid (?:tc rate|delay|jitter)"):
                apply_netem(namespace, interface, **values)
            run.assert_not_called()
        assert _state(namespace, interface) == before


@pytest.mark.parametrize("in_namespace", [False, True])
@pytest.mark.parametrize("field", ["rate", "delay", "jitter"])
@pytest.mark.parametrize(
    "number", ["\uff11", "\u0661", "\u0967", "1.\uff15", "\uff11.5", "1.\u0662"]
)
def test_non_ascii_digits_preserve_existing_qdisc(
    in_namespace: bool, field: str, number: str
) -> None:
    with _interface(in_namespace) as (namespace, interface):
        apply_netem(namespace, interface, rate="8mbit", delay="2ms", jitter="0.5ms")
        before = _state(namespace, interface)
        assert before is not None
        values = {field: number + ("mbit" if field == "rate" else "ms")}
        if field == "jitter":
            values["delay"] = "10ms"
        with patch.object(tc, "_run", wraps=tc._run) as run:
            with pytest.raises(TcError, match=r"invalid (?:tc rate|delay|jitter)"):
                apply_netem(namespace, interface, **values)
            run.assert_not_called()
        assert _state(namespace, interface) == before


@pytest.mark.parametrize("in_namespace", [False, True])
@pytest.mark.parametrize(("field", "value"), _INVALID_UNIT_FORMATS)
def test_unsupported_unit_formats_preserve_existing_qdisc(
    in_namespace: bool, field: str, value: str
) -> None:
    with _interface(in_namespace) as (namespace, interface):
        apply_netem(namespace, interface, rate="8mbit", delay="2ms", jitter="0.5ms")
        before = _state(namespace, interface)
        assert before is not None
        values = {field: value}
        if field == "jitter":
            values["delay"] = "10ms"
        with patch.object(tc, "_run", wraps=tc._run) as run:
            with pytest.raises(TcError, match=r"invalid (?:tc rate|delay|jitter)"):
                apply_netem(namespace, interface, **values)
            run.assert_not_called()
        assert _state(namespace, interface) == before


@pytest.mark.parametrize("uppercase", [False, True])
@pytest.mark.parametrize("in_namespace", [False, True])
@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("rate", "8mbit", 1_000_000),
        ("rate", "2.5mbit", 312_500),
        ("delay", "2ms", 0.002),
        ("delay", "0.5ms", 0.0005),
        ("jitter", "1ms", 0.001),
        ("jitter", "0.5ms", 0.0005),
    ],
)
def test_valid_strings_configure_kernel_and_clear(
    in_namespace: bool, field: str, value: str, expected: float, uppercase: bool
) -> None:
    if uppercase:
        value = value.upper()
    with _interface(in_namespace) as (namespace, interface):
        values = {field: value}
        if field == "jitter":
            values["delay"] = "10ms"
        apply_netem(namespace, interface, **values)
        state = _state(namespace, interface)
        assert state is not None
        options = state["options"]
        actual = options["rate"]["rate"] if field == "rate" else options["delay"][field]
        assert math.isclose(actual, expected, abs_tol=0.000001)
        clear_qdisc(namespace, interface)
        assert _state(namespace, interface) is None
