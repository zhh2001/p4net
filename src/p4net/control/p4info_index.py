"""Indexed, query-friendly view over a parsed `p4.config.v1.P4Info` message."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from p4net.control.codec import (
    canonicalize,
    encode_value,
    format_exact,
    format_lpm,
    format_range,
    format_ternary,
    parse_lpm,
    parse_range,
    parse_ternary,
)
from p4net.control.exceptions import (
    EncodingError,
    NoSuchActionError,
    NoSuchFieldError,
    NoSuchTableError,
    P4RuntimeError,
)


def _import_p4info() -> Any:
    from p4.config.v1 import p4info_pb2

    return p4info_pb2


def _import_p4runtime() -> Any:
    from p4.v1 import p4runtime_pb2

    return p4runtime_pb2


class P4InfoIndex:
    """Indexed view of a P4Info message with name → id and encoding helpers."""

    def __init__(self, p4info: Any) -> None:
        self._p4info = p4info
        self._tables_by_name: dict[str, Any] = {t.preamble.name: t for t in p4info.tables}
        self._actions_by_name: dict[str, Any] = {a.preamble.name: a for a in p4info.actions}
        self._counters_by_name: dict[str, Any] = {c.preamble.name: c for c in p4info.counters}
        self._packet_in: Any | None = None
        self._packet_out: Any | None = None
        for cpm in p4info.controller_packet_metadata:
            if cpm.preamble.name == "packet_in":
                self._packet_in = cpm
            elif cpm.preamble.name == "packet_out":
                self._packet_out = cpm

    @classmethod
    def from_file(cls, path: Path) -> P4InfoIndex:
        """Parse a P4Info text-protobuf file."""
        return cls.from_bytes(Path(path).read_bytes(), text_format=True)

    @classmethod
    def from_bytes(cls, data: bytes, *, text_format: bool = True) -> P4InfoIndex:
        """Parse from text or binary protobuf bytes."""
        p4info_pb2 = _import_p4info()
        msg = p4info_pb2.P4Info()
        if text_format:
            from google.protobuf import text_format as _tf

            _tf.Merge(data.decode("utf-8"), msg)
        else:
            msg.ParseFromString(data)
        return cls(msg)

    @property
    def raw(self) -> Any:
        return self._p4info

    @property
    def table_names(self) -> list[str]:
        return list(self._tables_by_name)

    @property
    def action_names(self) -> list[str]:
        return list(self._actions_by_name)

    # Lookup -------------------------------------------------------------

    def table_id(self, name: str) -> int:
        t = self._tables_by_name.get(name)
        if t is None:
            raise NoSuchTableError(f"no table named {name!r}")
        return int(t.preamble.id)

    def action_id(self, name: str) -> int:
        a = self._actions_by_name.get(name)
        if a is None:
            raise NoSuchActionError(f"no action named {name!r}")
        return int(a.preamble.id)

    def counter_id(self, name: str) -> int:
        c = self._counters_by_name.get(name)
        if c is None:
            raise P4RuntimeError(f"no counter named {name!r}")
        return int(c.preamble.id)

    def table_name(self, table_id: int) -> str:
        for t in self._tables_by_name.values():
            if t.preamble.id == table_id:
                return str(t.preamble.name)
        raise NoSuchTableError(f"no table with id {table_id}")

    def action_name(self, action_id: int) -> str:
        for a in self._actions_by_name.values():
            if a.preamble.id == action_id:
                return str(a.preamble.name)
        raise NoSuchActionError(f"no action with id {action_id}")

    def counter_name(self, counter_id: int) -> str:
        for c in self._counters_by_name.values():
            if c.preamble.id == counter_id:
                return str(c.preamble.name)
        raise P4RuntimeError(f"no counter with id {counter_id}")

    def table_requires_priority(self, name: str) -> bool:
        """True iff the table has a TERNARY or RANGE match field."""
        t = self._tables_by_name.get(name)
        if t is None:
            raise NoSuchTableError(f"no table named {name!r}")
        p4info_pb2 = _import_p4info()
        return any(
            mf.match_type in (p4info_pb2.MatchField.TERNARY, p4info_pb2.MatchField.RANGE)
            for mf in t.match_fields
        )

    def multicast_group_id_unused(self) -> int:
        """Helper: return 1. The actual selection is delegated to the controller."""
        return 1

    # Encoding -----------------------------------------------------------

    def encode_match(
        self,
        table_name: str,
        match: Mapping[str, object],
    ) -> list[Any]:
        """Build a list of P4Runtime FieldMatch protos for the given table."""
        table = self._tables_by_name.get(table_name)
        if table is None:
            raise NoSuchTableError(f"no table named {table_name!r}")
        p4info_pb2 = _import_p4info()
        p4runtime_pb2 = _import_p4runtime()

        fields_by_name: dict[str, Any] = {mf.name: mf for mf in table.match_fields}
        for key in match:
            if key not in fields_by_name:
                raise NoSuchFieldError(f"field {key!r} not present in table {table_name!r}")
        for mf in table.match_fields:
            if mf.match_type == p4info_pb2.MatchField.EXACT and mf.name not in match:
                raise EncodingError(
                    f"required exact match field {mf.name!r} missing for table {table_name!r}"
                )

        result: list[Any] = []
        for mf_name, value in match.items():
            mf = fields_by_name[mf_name]
            fm = p4runtime_pb2.FieldMatch()
            fm.field_id = int(mf.id)
            bw = int(mf.bitwidth)
            mt = mf.match_type
            if mt == p4info_pb2.MatchField.EXACT:
                fm.exact.value = canonicalize(encode_value(value, bw))  # type: ignore[arg-type]
            elif mt == p4info_pb2.MatchField.LPM:
                encoded, plen = parse_lpm(value, bw)  # type: ignore[arg-type]
                if plen == 0:
                    # P4Runtime: a missing LPM field is wildcard; encoding plen=0 here would
                    # be rejected by the switch. Skip the field instead.
                    continue
                fm.lpm.value = canonicalize(encoded)
                fm.lpm.prefix_len = plen
            elif mt == p4info_pb2.MatchField.TERNARY:
                v, m = parse_ternary(value, bw)  # type: ignore[arg-type]
                if all(b == 0 for b in m):
                    continue  # all-zero mask is wildcard; omit
                fm.ternary.value = canonicalize(v)
                fm.ternary.mask = canonicalize(m)
            elif mt == p4info_pb2.MatchField.RANGE:
                low, high = parse_range(value, bw)  # type: ignore[arg-type]
                fm.range.low = canonicalize(low)
                fm.range.high = canonicalize(high)
            elif mt == p4info_pb2.MatchField.OPTIONAL:
                fm.optional.value = canonicalize(encode_value(value, bw))  # type: ignore[arg-type]
            else:
                raise EncodingError(f"unsupported match type {mt} for field {mf_name!r}")
            result.append(fm)
        return result

    def decode_match(
        self,
        table_name: str,
        match: Mapping[str, object],
    ) -> dict[str, str]:
        """Inverse of `encode_match`: render raw match bytes as human strings.

        For each ``(field_name, raw_value)`` pair the field's bitwidth and
        match type are looked up in the P4Info, then the value is formatted:

        - 32-bit fields → IPv4 dotted-quad (with ``/<plen>`` for LPM,
          ``&<mask>`` for TERNARY, ``[<lo>,<hi>]`` for RANGE).
        - 48-bit fields → MAC ``xx:xx:xx:xx:xx:xx`` (with the same combinators).
        - Other widths → decimal int (with the same combinators).

        Width is taken from P4Info; the bytes may be canonical (shorter than
        width-rounded) and are zero-extended on the high side before decoding.
        """
        table = self._tables_by_name.get(table_name)
        if table is None:
            raise NoSuchTableError(f"no table named {table_name!r}")
        p4info_pb2 = _import_p4info()
        fields_by_name: dict[str, Any] = {mf.name: mf for mf in table.match_fields}
        out: dict[str, str] = {}
        for name, raw in match.items():
            mf = fields_by_name.get(name)
            if mf is None:
                raise NoSuchFieldError(f"field {name!r} not present in table {table_name!r}")
            bw = int(mf.bitwidth)
            mt = mf.match_type
            if mt == p4info_pb2.MatchField.EXACT or mt == p4info_pb2.MatchField.OPTIONAL:
                if not isinstance(raw, bytes):
                    raise EncodingError(
                        f"field {name!r} expected bytes for EXACT/OPTIONAL, "
                        f"got {type(raw).__name__}"
                    )
                out[name] = format_exact(raw, bw)
            elif mt == p4info_pb2.MatchField.LPM:
                if not (isinstance(raw, tuple) and len(raw) == 2):
                    raise EncodingError(
                        f"field {name!r} expected (bytes, plen) for LPM, got {raw!r}"
                    )
                value, plen = raw
                if not isinstance(value, bytes) or not isinstance(plen, int):
                    raise EncodingError(f"field {name!r}: bad LPM tuple {raw!r}")
                out[name] = format_lpm(value, plen, bw)
            elif mt == p4info_pb2.MatchField.TERNARY:
                if not (isinstance(raw, tuple) and len(raw) == 2):
                    raise EncodingError(
                        f"field {name!r} expected (bytes, bytes) for TERNARY, got {raw!r}"
                    )
                v, m = raw
                if not isinstance(v, bytes) or not isinstance(m, bytes):
                    raise EncodingError(f"field {name!r}: bad TERNARY tuple {raw!r}")
                out[name] = format_ternary(v, m, bw)
            elif mt == p4info_pb2.MatchField.RANGE:
                if not (isinstance(raw, tuple) and len(raw) == 2):
                    raise EncodingError(
                        f"field {name!r} expected (bytes, bytes) for RANGE, got {raw!r}"
                    )
                lo, hi = raw
                if not isinstance(lo, bytes) or not isinstance(hi, bytes):
                    raise EncodingError(f"field {name!r}: bad RANGE tuple {raw!r}")
                out[name] = format_range(lo, hi, bw)
            else:
                raise EncodingError(f"unsupported match type {mt} for field {name!r}")
        return out

    def encode_action(
        self,
        action_name: str,
        params: Mapping[str, object] | None = None,
    ) -> Any:
        """Build a P4Runtime Action proto."""
        action = self._actions_by_name.get(action_name)
        if action is None:
            raise NoSuchActionError(f"no action named {action_name!r}")
        p4runtime_pb2 = _import_p4runtime()
        params = params or {}
        params_by_name: dict[str, Any] = {p.name: p for p in action.params}
        for name in params:
            if name not in params_by_name:
                raise NoSuchFieldError(f"param {name!r} not present in action {action_name!r}")
        for p in action.params:
            if p.name not in params:
                raise EncodingError(f"required action param {p.name!r} missing for {action_name!r}")
        action_proto = p4runtime_pb2.Action()
        action_proto.action_id = int(action.preamble.id)
        for p in action.params:
            ap = action_proto.params.add()
            ap.param_id = int(p.id)
            ap.value = canonicalize(encode_value(params[p.name], int(p.bitwidth)))  # type: ignore[arg-type]
        return action_proto

    # Controller packet metadata -----------------------------------------

    def packet_in_metadata_schema(self) -> list[tuple[str, int]]:
        """Return ``[(name, bitwidth), ...]`` for the packet_in controller header.

        Empty list if the program declares no ``@controller_header("packet_in")``.
        """
        if self._packet_in is None:
            return []
        return [(m.name, int(m.bitwidth)) for m in self._packet_in.metadata]

    def packet_out_metadata_schema(self) -> list[tuple[str, int]]:
        """Return ``[(name, bitwidth), ...]`` for the packet_out controller header.

        Empty list if the program declares no ``@controller_header("packet_out")``.
        """
        if self._packet_out is None:
            return []
        return [(m.name, int(m.bitwidth)) for m in self._packet_out.metadata]

    def encode_packet_out_metadata(self, metadata: Mapping[str, object]) -> list[Any]:
        """Encode controller metadata for a PacketOut.

        Each ``(key, value)`` pair is matched against the packet_out schema;
        values go through ``encode_value`` with the schema's bitwidth, then
        wrapped in a ``PacketMetadata`` proto with the corresponding numeric id
        (P4Runtime uses ids on the wire, not field names). Missing keys are
        populated with zero-valued bytes of the correct width. Raises
        ``NoSuchFieldError`` on unknown keys, ``EncodingError`` on width
        overflow.
        """
        if self._packet_out is None:
            if metadata:
                raise NoSuchFieldError(
                    "P4Info declares no packet_out controller header; "
                    f"cannot encode metadata {dict(metadata)!r}"
                )
            return []
        p4runtime_pb2 = _import_p4runtime()
        schema_by_name: dict[str, Any] = {m.name: m for m in self._packet_out.metadata}
        for k in metadata:
            if k not in schema_by_name:
                raise NoSuchFieldError(f"field {k!r} not present in packet_out controller header")
        out: list[Any] = []
        for m in self._packet_out.metadata:
            pm = p4runtime_pb2.PacketMetadata()
            pm.metadata_id = int(m.id)
            value = metadata.get(m.name, 0)
            pm.value = canonicalize(encode_value(value, int(m.bitwidth)))  # type: ignore[arg-type]
            out.append(pm)
        return out

    def decode_packet_in_metadata(self, metadata: Any) -> dict[str, int]:
        """Decode PacketIn controller metadata into ``{field_name: int}``.

        Unknown ids (e.g. switch running a different P4Info than the controller
        has loaded) are silently dropped; the dispatcher logs them at DEBUG.
        """
        if self._packet_in is None:
            return {}
        schema_by_id: dict[int, Any] = {int(m.id): m for m in self._packet_in.metadata}
        out: dict[str, int] = {}
        for pm in metadata:
            mid = int(pm.metadata_id)
            m = schema_by_id.get(mid)
            if m is None:
                continue
            out[str(m.name)] = int.from_bytes(bytes(pm.value), "big")
        return out
