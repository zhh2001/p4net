"""`P4Switch` descriptor: a BMv2 simple_switch_grpc instance to be brought up."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from p4net.topo.exceptions import TopologyError
from p4net.topo.host import NAME_RE

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"trace", "debug", "info", "warn", "error"})
_DEVICE_ID_MAX = 2**31
_PORT_MIN = 1024
_PORT_MAX = 65535
_CPU_PORT_MIN = 1
_CPU_PORT_MAX = 510


@dataclass(frozen=True)
class P4Switch:
    """A P4-programmable switch backed by BMv2 simple_switch_grpc."""

    name: str
    p4_src: Path
    arch: str = "v1model"
    device_id: int | None = None
    grpc_port: int | None = None
    thrift_port: int | None = None
    cpu_port: int | None = None
    log_level: str = "info"
    pcap_enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not NAME_RE.match(self.name):
            raise TopologyError(f"invalid switch name {self.name!r}: must match {NAME_RE.pattern}")
        # Coerce p4_src to Path (frozen dataclass: bypass via object.__setattr__).
        if not isinstance(self.p4_src, Path):
            object.__setattr__(self, "p4_src", Path(self.p4_src))
        if self.p4_src.suffix != ".p4":
            raise TopologyError(f"p4_src {str(self.p4_src)!r}: file must have .p4 suffix")
        if self.arch != "v1model":
            raise TopologyError(
                f"unsupported arch {self.arch!r}: only 'v1model' is supported in this release"
            )
        if self.device_id is not None and not 0 <= self.device_id < _DEVICE_ID_MAX:
            raise TopologyError(f"device_id {self.device_id} out of range [0, {_DEVICE_ID_MAX})")
        if self.grpc_port is not None and not _PORT_MIN <= self.grpc_port <= _PORT_MAX:
            raise TopologyError(
                f"grpc_port {self.grpc_port} out of range [{_PORT_MIN}, {_PORT_MAX}]"
            )
        if self.thrift_port is not None and not _PORT_MIN <= self.thrift_port <= _PORT_MAX:
            raise TopologyError(
                f"thrift_port {self.thrift_port} out of range [{_PORT_MIN}, {_PORT_MAX}]"
            )
        if self.cpu_port is not None and not _CPU_PORT_MIN <= self.cpu_port <= _CPU_PORT_MAX:
            raise TopologyError(
                f"cpu_port {self.cpu_port} out of range [{_CPU_PORT_MIN}, {_CPU_PORT_MAX}]"
            )
        if self.log_level not in _VALID_LOG_LEVELS:
            raise TopologyError(
                f"invalid log_level {self.log_level!r}: must be one of {sorted(_VALID_LOG_LEVELS)}"
            )
