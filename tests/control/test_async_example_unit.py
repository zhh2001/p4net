"""Failure cleanup for the async concurrent example."""

from __future__ import annotations

import asyncio
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples/async_concurrent/topology.py"


@pytest.mark.parametrize("phase", ["connect", "insert_table_entry"])
async def test_async_example_drains_pending_operations_before_disconnect(phase: str) -> None:
    setup = run_path(str(_EXAMPLE))["_async_setup"]
    peer_started = asyncio.Event()
    peers: list[asyncio.Task[None]] = []
    disconnect_states: list[bool] = []
    failure = RuntimeError(f"{phase} failed")
    clients = {name: Mock() for name in ("s1", "s2", "s3")}

    async def fail(*args: object) -> None:
        await peer_started.wait()
        raise failure

    async def wait_for_cancel(*args: object) -> None:
        task = asyncio.current_task()
        assert task is not None
        peers.append(task)
        peer_started.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)

    async def disconnect() -> None:
        disconnect_states.append(all(task.cancelled() for task in peers))

    for client in clients.values():
        client.connect = AsyncMock()
        client.insert_table_entry = AsyncMock()
        client.disconnect = AsyncMock(side_effect=disconnect)
    getattr(clients["s1"], phase).side_effect = fail
    getattr(clients["s2"], phase).side_effect = wait_for_cancel
    switches = {
        name: SimpleNamespace(name=name, async_client=client) for name, client in clients.items()
    }
    network = SimpleNamespace(switch=switches.__getitem__)

    try:
        with pytest.raises(RuntimeError) as caught:
            await asyncio.wait_for(setup(network), timeout=5)

        assert caught.value is failure
        assert disconnect_states == [True] * len(clients)
        for client in clients.values():
            client.disconnect.assert_awaited_once()
    finally:
        for task in peers:
            task.cancel()
        await asyncio.gather(*peers, return_exceptions=True)
