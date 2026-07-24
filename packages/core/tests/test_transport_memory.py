"""Tests for the Transport protocol and in-memory transport pair."""

import asyncio
from typing import Any

import pytest
from labwire.core.transport import MemoryTransport, Transport, TransportClosed


def test_memory_transport_satisfies_transport_protocol() -> None:
    a, b = MemoryTransport.pair()
    assert isinstance(a, Transport)
    assert isinstance(b, Transport)


async def test_messages_flow_both_directions_in_order() -> None:
    a, b = MemoryTransport.pair()
    await a.send({"n": 1})
    await a.send({"n": 2})
    await b.send({"reply": True})
    assert await b.receive() == {"n": 1}
    assert await b.receive() == {"n": 2}
    assert await a.receive() == {"reply": True}


async def test_receive_raises_after_peer_closes() -> None:
    a, b = MemoryTransport.pair()
    await a.close()
    with pytest.raises(TransportClosed):
        await b.receive()


async def test_send_after_close_raises() -> None:
    a, b = MemoryTransport.pair()
    await a.close()
    with pytest.raises(TransportClosed):
        await a.send({"n": 1})
    with pytest.raises(TransportClosed):
        await b.send({"n": 1})


async def test_close_is_idempotent() -> None:
    a, _b = MemoryTransport.pair()
    await a.close()
    await a.close()


async def test_pending_receive_wakes_on_close() -> None:
    a, b = MemoryTransport.pair()

    async def receiver() -> dict[str, Any]:
        return await b.receive()

    task = asyncio.create_task(receiver())
    await asyncio.sleep(0.01)
    await a.close()
    with pytest.raises(TransportClosed):
        await task


async def test_queued_messages_drain_before_closed_raises() -> None:
    a, b = MemoryTransport.pair()
    await a.send({"n": 1})
    await a.close()
    assert await b.receive() == {"n": 1}
    with pytest.raises(TransportClosed):
        await b.receive()
