"""Unit tests for `p4net.topo.switch`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from p4net.topo import P4Switch, Topology, TopologyError


def _make_numeric_switch(field: str, value: Any, source: str) -> P4Switch:
    values = {field: value}
    if source == "descriptor":
        return P4Switch("s1", Path("p.p4"), **values)
    if source == "builder":
        return Topology().add_switch("s1", Path("p.p4"), **values)
    data = {"switches": {"s1": {"name": "s1", "p4_src": "p.p4", **values}}}
    return Topology.from_dict(data).switches["s1"]


_INVALID_NUMERIC_FIELDS = [
    (field, value)
    for field, integer, fraction in (
        ("device_id", 0.0, 1.5),
        ("grpc_port", 50051.0, 50051.5),
        ("thrift_port", 9090.0, 9090.5),
    )
    for value in (
        True,
        False,
        integer,
        fraction,
        "1",
        b"1",
        float("nan"),
        float("inf"),
        -float("inf"),
        [],
        {},
    )
]


@pytest.mark.parametrize("source", ["descriptor", "builder", "dictionary"])
@pytest.mark.parametrize(("field", "value"), _INVALID_NUMERIC_FIELDS)
def test_switch_numeric_fields_reject_invalid_types(field: str, value: Any, source: str) -> None:
    with pytest.raises(TopologyError, match=field):
        _make_numeric_switch(field, value, source)


@pytest.mark.parametrize("source", ["descriptor", "builder", "dictionary"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("device_id", None),
        ("device_id", 0),
        ("device_id", 17),
        ("device_id", (1 << 31) - 1),
        ("grpc_port", None),
        ("grpc_port", 1024),
        ("grpc_port", 50051),
        ("grpc_port", 65535),
        ("thrift_port", None),
        ("thrift_port", 1024),
        ("thrift_port", 9090),
        ("thrift_port", 65535),
    ],
)
def test_switch_numeric_fields_preserve_valid_values(
    field: str, value: int | None, source: str
) -> None:
    switch = _make_numeric_switch(field, value, source)
    expected = (
        {"device_id": 0, "grpc_port": 50051, "thrift_port": 9090}[field]
        if value is None and source == "builder"
        else value
    )
    actual = getattr(switch, field)
    assert actual == expected
    assert actual is None or type(actual) is int


@pytest.mark.parametrize("source", ["descriptor", "builder", "dictionary"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("device_id", -1),
        ("device_id", 1 << 31),
        ("grpc_port", 1023),
        ("grpc_port", 65536),
        ("thrift_port", 1023),
        ("thrift_port", 65536),
    ],
)
def test_switch_numeric_fields_keep_existing_ranges(field: str, value: int, source: str) -> None:
    with pytest.raises(TopologyError, match=field):
        _make_numeric_switch(field, value, source)


def test_minimal_switch() -> None:
    s = P4Switch(name="s1", p4_src=Path("prog.p4"))
    assert s.name == "s1"
    assert s.p4_src == Path("prog.p4")
    assert s.arch == "v1model"
    assert s.log_level == "info"
    assert s.pcap_enabled is True


def test_p4_src_string_coerced_to_path() -> None:
    s = P4Switch(name="s1", p4_src="prog.p4")  # type: ignore[arg-type]
    assert isinstance(s.p4_src, Path)
    assert str(s.p4_src) == "prog.p4"


@pytest.mark.parametrize("bad_name", ["", "1s", "with space", "x" * 13])
def test_invalid_switch_name(bad_name: str) -> None:
    with pytest.raises(TopologyError):
        P4Switch(name=bad_name, p4_src=Path("p.p4"))


def test_rejects_missing_p4_suffix() -> None:
    with pytest.raises(TopologyError, match=r"\.p4 suffix"):
        P4Switch(name="s1", p4_src=Path("prog.txt"))


def test_rejects_non_v1model_arch() -> None:
    with pytest.raises(TopologyError, match="only 'v1model'"):
        P4Switch(name="s1", p4_src=Path("p.p4"), arch="psa")


@pytest.mark.parametrize("bad_id", [-1, 2**31, 2**40])
def test_invalid_device_id(bad_id: int) -> None:
    with pytest.raises(TopologyError):
        P4Switch(name="s1", p4_src=Path("p.p4"), device_id=bad_id)


def test_valid_device_id_zero() -> None:
    P4Switch(name="s1", p4_src=Path("p.p4"), device_id=0)


@pytest.mark.parametrize("bad_port", [0, 80, 1023, 65536, 70000])
def test_invalid_grpc_port(bad_port: int) -> None:
    with pytest.raises(TopologyError):
        P4Switch(name="s1", p4_src=Path("p.p4"), grpc_port=bad_port)


@pytest.mark.parametrize("bad_port", [0, 80, 1023, 65536])
def test_invalid_thrift_port(bad_port: int) -> None:
    with pytest.raises(TopologyError):
        P4Switch(name="s1", p4_src=Path("p.p4"), thrift_port=bad_port)


@pytest.mark.parametrize("good_port", [1024, 50051, 65535])
def test_valid_grpc_and_thrift_ports(good_port: int) -> None:
    P4Switch(name="s1", p4_src=Path("p.p4"), grpc_port=good_port, thrift_port=good_port)


@pytest.mark.parametrize("bad_cpu", [0, 511, 1024, -1])
def test_invalid_cpu_port(bad_cpu: int) -> None:
    with pytest.raises(TopologyError):
        P4Switch(name="s1", p4_src=Path("p.p4"), cpu_port=bad_cpu)


@pytest.mark.parametrize("good_cpu", [1, 255, 510])
def test_valid_cpu_port(good_cpu: int) -> None:
    P4Switch(name="s1", p4_src=Path("p.p4"), cpu_port=good_cpu)


@pytest.mark.parametrize(
    "cpu_port", [True, False, 1.0, 1.5, 510.0, "1", b"1", float("nan"), float("inf"), [], {}]
)
def test_cpu_port_rejects_invalid_types(cpu_port: Any) -> None:
    with pytest.raises(TopologyError, match="cpu_port"):
        P4Switch("s1", Path("p.p4"), cpu_port=cpu_port)


@pytest.mark.parametrize("bad_level", ["", "verbose", "FATAL", "Info"])
def test_invalid_log_level(bad_level: str) -> None:
    with pytest.raises(TopologyError):
        P4Switch(name="s1", p4_src=Path("p.p4"), log_level=bad_level)


@pytest.mark.parametrize("good_level", ["trace", "debug", "info", "warn", "error"])
def test_valid_log_level(good_level: str) -> None:
    P4Switch(name="s1", p4_src=Path("p.p4"), log_level=good_level)
