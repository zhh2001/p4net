"""Unit tests for `P4RuntimeClient.write_register` and `read_register`.

The register methods talk to BMv2's Thrift control channel via the
``simple_switch_CLI`` subprocess; gRPC is mocked for the connection
handshake, and ``subprocess.run`` is mocked for the actual register call.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from p4.config.v1 import p4info_pb2
from p4.v1 import p4runtime_pb2
from pytest_mock import MockerFixture

import p4net.control  # noqa: F401  (sets protobuf python-impl env var)
from p4net.control import (
    EncodingError,
    NoSuchRegisterError,
    P4RuntimeClient,
    P4RuntimeError,
)

# ---------------------------------------------------------------------------
# Fake gRPC plumbing (mirrors test_client_unit.py).
# ---------------------------------------------------------------------------


class FakeStreamCall:
    def __init__(self) -> None:
        self.responses: queue.Queue[Any] = queue.Queue()
        self.requests_seen: list[Any] = []
        self._cancelled = False
        self._reader: threading.Thread | None = None
        self._request_iter: Iterator[Any] | None = None

    def attach_request_iter(self, request_iter: Iterator[Any]) -> None:
        self._request_iter = request_iter
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self._request_iter is not None
        try:
            for req in self._request_iter:
                self.requests_seen.append(req)
                if self._cancelled:
                    break
        except Exception:
            pass

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        item = self.responses.get()
        if item is StopIteration:
            raise StopIteration
        return item

    def cancel(self) -> None:
        self._cancelled = True
        self.responses.put(StopIteration)


def _make_arbitration_response(device_id: int, election_id: tuple[int, int]) -> Any:
    resp = p4runtime_pb2.StreamMessageResponse()
    resp.arbitration.device_id = device_id
    resp.arbitration.election_id.high = election_id[0]
    resp.arbitration.election_id.low = election_id[1]
    resp.arbitration.status.code = 0
    return resp


def _build_p4info_with_register(
    *,
    bitwidth: int = 32,
    size: int = 16,
) -> p4info_pb2.P4Info:
    p = p4info_pb2.P4Info()
    r = p.registers.add()
    r.preamble.id = 4001
    r.preamble.name = "MyIngress.test_register"
    r.type_spec.bitstring.bit.bitwidth = bitwidth
    r.size = size
    return p


@pytest.fixture
def patched_grpc(mocker: MockerFixture) -> dict[str, Any]:
    chan = MagicMock(name="grpc-channel")
    stub = MagicMock(name="P4RuntimeStub")
    stream = FakeStreamCall()

    def stream_channel(request_iter: Iterator[Any]) -> FakeStreamCall:
        stream.attach_request_iter(request_iter)
        return stream

    stub.StreamChannel = MagicMock(side_effect=stream_channel)
    mocker.patch("p4net.control.client.grpc.insecure_channel", return_value=chan)
    mocker.patch(
        "p4net.control.client.p4runtime_pb2_grpc.P4RuntimeStub",
        return_value=stub,
    )
    return {"channel": chan, "stub": stub, "stream": stream}


@pytest.fixture
def connected_client(
    patched_grpc: dict[str, Any], tmp_path: Path
) -> tuple[P4RuntimeClient, dict[str, Any], Path, Path]:
    stream: FakeStreamCall = patched_grpc["stream"]
    stream.responses.put(_make_arbitration_response(0, (1, 0)))
    client = P4RuntimeClient(
        "127.0.0.1:50051",
        device_id=0,
        thrift_address=("127.0.0.1", 9090),
    )
    client.connect(timeout=2.0)
    p4info = _build_p4info_with_register(bitwidth=32, size=16)
    p4info_path = tmp_path / "p.p4info.txtpb"
    from google.protobuf import text_format

    p4info_path.write_text(text_format.MessageToString(p4info))
    json_path = tmp_path / "p.json"
    json_path.write_bytes(b'{"fake":"json"}')
    return client, patched_grpc, p4info_path, json_path


def _push_pipeline(
    bundle: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> P4RuntimeClient:
    client, grpc_mocks, p4info_path, json_path = bundle
    grpc_mocks["stub"].SetForwardingPipelineConfig = MagicMock(return_value=MagicMock())
    client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path, timeout=2.0)
    return client


# ---------------------------------------------------------------------------
# Helper: patch subprocess.run for the Thrift CLI shell-out.
# ---------------------------------------------------------------------------


def _patch_thrift_cli(mocker: MockerFixture, stdout: str, returncode: int = 0) -> MagicMock:
    """Replace subprocess.run with a stub that returns ``stdout`` / ``returncode``."""

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        return MagicMock(stdout=stdout, stderr="", returncode=returncode)

    return mocker.patch("subprocess.run", side_effect=fake_run)


# ---------------------------------------------------------------------------
# write_register
# ---------------------------------------------------------------------------


def test_write_register_shells_out_with_correct_command(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
) -> None:
    client = _push_pipeline(connected_client)
    run = _patch_thrift_cli(mocker, stdout="RuntimeCmd:\n")
    try:
        client.write_register("MyIngress.test_register", index=3, value=0xCAFE)
        argv = run.call_args.args[0]
        assert argv[0] == "simple_switch_CLI"
        assert "--thrift-port" in argv
        assert "9090" in argv
        # stdin carries the register_write command in decimal form.
        stdin = run.call_args.kwargs["input"]
        assert stdin.strip() == "register_write MyIngress.test_register 3 51966"
    finally:
        client.disconnect()


def test_write_register_index_out_of_range_raises(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
) -> None:
    client = _push_pipeline(connected_client)
    run = _patch_thrift_cli(mocker, stdout="")
    try:
        with pytest.raises(EncodingError, match=r"out of range \[0, 16\)"):
            client.write_register("MyIngress.test_register", index=16, value=0)
        run.assert_not_called()
    finally:
        client.disconnect()


def test_write_register_value_exceeds_bitwidth_raises(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
) -> None:
    client = _push_pipeline(connected_client)
    run = _patch_thrift_cli(mocker, stdout="")
    try:
        with pytest.raises(EncodingError, match="does not fit in 32 bits"):
            client.write_register("MyIngress.test_register", index=0, value=2**32)
        run.assert_not_called()
    finally:
        client.disconnect()


def test_write_register_unknown_name_raises(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    try:
        with pytest.raises(NoSuchRegisterError, match="no register named 'missing'"):
            client.write_register("missing", index=0, value=0)
    finally:
        client.disconnect()


def test_write_register_without_thrift_address_raises(
    patched_grpc: dict[str, Any], tmp_path: Path
) -> None:
    """A client constructed without thrift_address can't reach BMv2 for registers."""
    stream: FakeStreamCall = patched_grpc["stream"]
    stream.responses.put(_make_arbitration_response(0, (1, 0)))
    client = P4RuntimeClient("127.0.0.1:50051", device_id=0)
    client.connect(timeout=2.0)
    try:
        # Push pipeline so the index is loaded.
        p4info = _build_p4info_with_register()
        p4info_path = tmp_path / "p.p4info.txtpb"
        from google.protobuf import text_format

        p4info_path.write_text(text_format.MessageToString(p4info))
        json_path = tmp_path / "p.json"
        json_path.write_bytes(b'{"fake":"json"}')
        patched_grpc["stub"].SetForwardingPipelineConfig = MagicMock(return_value=MagicMock())
        client.set_pipeline_config(bmv2_json=json_path, p4info=p4info_path, timeout=2.0)
        with pytest.raises(P4RuntimeError, match="require a Thrift sidecar address"):
            client.write_register("MyIngress.test_register", index=0, value=1)
    finally:
        client.disconnect()


# ---------------------------------------------------------------------------
# read_register
# ---------------------------------------------------------------------------


def test_read_register_single_index_returns_int(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
) -> None:
    client = _push_pipeline(connected_client)
    _patch_thrift_cli(
        mocker,
        stdout="RuntimeCmd: MyIngress.test_register[5]= 48879\nRuntimeCmd:\n",
    )
    try:
        out = client.read_register("MyIngress.test_register", index=5)
        assert out == 48879
    finally:
        client.disconnect()


def test_read_register_no_index_returns_full_list(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
) -> None:
    client = _push_pipeline(connected_client)
    _patch_thrift_cli(
        mocker,
        stdout=(
            "RuntimeCmd: MyIngress.test_register= 10, 0, 0, 30, 0, 0, 0, 70, "
            "0, 0, 0, 0, 0, 0, 0, 0\nRuntimeCmd:\n"
        ),
    )
    try:
        out = client.read_register("MyIngress.test_register")
        assert isinstance(out, list)
        assert len(out) == 16
        assert out[0] == 10
        assert out[3] == 30
        assert out[7] == 70
        assert all(out[i] == 0 for i in (1, 2, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15))
    finally:
        client.disconnect()


def test_read_register_index_out_of_range_raises(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
) -> None:
    client = _push_pipeline(connected_client)
    run = _patch_thrift_cli(mocker, stdout="")
    try:
        with pytest.raises(EncodingError, match=r"out of range \[0, 16\)"):
            client.read_register("MyIngress.test_register", index=99)
        run.assert_not_called()
    finally:
        client.disconnect()


def test_read_register_unknown_name_raises(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
) -> None:
    client = _push_pipeline(connected_client)
    try:
        with pytest.raises(NoSuchRegisterError, match="no register named 'missing'"):
            client.read_register("missing")
    finally:
        client.disconnect()


def test_read_register_array_size_mismatch_raises(
    connected_client: tuple[P4RuntimeClient, dict[str, Any], Path, Path],
    mocker: MockerFixture,
) -> None:
    client = _push_pipeline(connected_client)
    _patch_thrift_cli(mocker, stdout="RuntimeCmd: MyIngress.test_register= 1, 2, 3\n")
    try:
        with pytest.raises(P4RuntimeError, match="expected 16 cells, got 3"):
            client.read_register("MyIngress.test_register")
    finally:
        client.disconnect()
