"""Decode per-update status codes from P4Runtime RPC errors."""

from __future__ import annotations

import grpc
from p4.v1 import p4runtime_pb2


def _extract_p4_canonical_codes(exc: grpc.RpcError) -> list[int]:
    """Pull per-update `p4.v1.Error.canonical_code` ints out of a gRPC error."""
    codes: list[int] = []
    try:
        metadata = exc.trailing_metadata() or ()
    except Exception:
        return codes
    for key, value in metadata:
        if key.lower() != "grpc-status-details-bin":
            continue
        try:
            from google.rpc import status_pb2

            rpc_status = status_pb2.Status()
            rpc_status.MergeFromString(value)
        except Exception:
            continue
        for detail in rpc_status.details:
            try:
                if detail.Is(p4runtime_pb2.Error.DESCRIPTOR):
                    err = p4runtime_pb2.Error()
                    detail.Unpack(err)
                    codes.append(int(err.canonical_code))
            except Exception:
                continue
    return codes


def _grpc_code_for(canonical: int) -> grpc.StatusCode | None:
    for sc in grpc.StatusCode:
        if sc.value[0] == canonical:
            return sc
    return None
