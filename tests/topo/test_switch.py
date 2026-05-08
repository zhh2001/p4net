"""Unit tests for `p4net.topo.switch`."""

from __future__ import annotations

from pathlib import Path

import pytest

from p4net.topo import P4Switch, TopologyError


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


@pytest.mark.parametrize("bad_level", ["", "verbose", "FATAL", "Info"])
def test_invalid_log_level(bad_level: str) -> None:
    with pytest.raises(TopologyError):
        P4Switch(name="s1", p4_src=Path("p.p4"), log_level=bad_level)


@pytest.mark.parametrize("good_level", ["trace", "debug", "info", "warn", "error"])
def test_valid_log_level(good_level: str) -> None:
    P4Switch(name="s1", p4_src=Path("p.p4"), log_level=good_level)
