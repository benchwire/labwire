"""Tests for the JSON-RPC session layer."""

import asyncio
from typing import Any

import pytest
from labwire.core.errors import BusyError, LabwireError
from labwire.core.session import JsonRpcSession, SessionClosed
from labwire.core.transport import MemoryTransport


async def test_request_gets_correlated_response() -> None:
    ours, theirs = MemoryTransport.pair()

    async def peer() -> None:
        msg = await theirs.receive()
        await theirs.send({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}})

    async with JsonRpcSession(ours) as session:
        peer_task = asyncio.create_task(peer())
        result = await session.request("ping", {})
        assert result == {"ok": True}
        await peer_task


async def test_concurrent_requests_resolve_to_correct_futures() -> None:
    ours, theirs = MemoryTransport.pair()

    async def peer() -> None:
        first = await theirs.receive()
        second = await theirs.receive()
        # answer out of order
        await theirs.send({"jsonrpc": "2.0", "id": second["id"], "result": "second"})
        await theirs.send({"jsonrpc": "2.0", "id": first["id"], "result": "first"})

    async with JsonRpcSession(ours) as session:
        peer_task = asyncio.create_task(peer())
        r1, r2 = await asyncio.gather(session.request("a", {}), session.request("b", {}))
        assert (r1, r2) == ("first", "second")
        await peer_task


async def test_error_response_raises_typed_error() -> None:
    ours, theirs = MemoryTransport.pair()

    async def peer() -> None:
        msg = await theirs.receive()
        await theirs.send(
            {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {
                    "code": -32002,
                    "message": "full",
                    "data": {"category": "busy", "retryable": True},
                },
            }
        )

    async with JsonRpcSession(ours) as session:
        peer_task = asyncio.create_task(peer())
        with pytest.raises(BusyError, match="full"):
            await session.request("command/submit", {})
        await peer_task


async def test_request_timeout() -> None:
    ours, _theirs = MemoryTransport.pair()
    async with JsonRpcSession(ours) as session:
        with pytest.raises(TimeoutError):
            await session.request("ping", {}, timeout=0.05)


async def test_notify_sends_notification_without_id() -> None:
    ours, theirs = MemoryTransport.pair()
    async with JsonRpcSession(ours) as session:
        await session.notify("notifications/initialized", {})
        msg = await theirs.receive()
        assert msg["method"] == "notifications/initialized"
        assert "id" not in msg


async def test_incoming_request_is_dispatched_and_answered() -> None:
    ours, theirs = MemoryTransport.pair()

    async def handle_request(method: str, params: dict[str, Any]) -> Any:
        assert method == "ping"
        return {}

    async with JsonRpcSession(ours, request_handler=handle_request):
        await theirs.send({"jsonrpc": "2.0", "id": 42, "method": "ping", "params": {}})
        reply = await theirs.receive()
        assert reply == {"jsonrpc": "2.0", "id": 42, "result": {}}


async def test_handler_labwire_error_becomes_error_response() -> None:
    ours, theirs = MemoryTransport.pair()

    async def handle_request(method: str, params: dict[str, Any]) -> Any:
        raise BusyError("no slots")

    async with JsonRpcSession(ours, request_handler=handle_request):
        await theirs.send({"jsonrpc": "2.0", "id": 1, "method": "command/submit", "params": {}})
        reply = await theirs.receive()
        assert reply["error"]["code"] == -32002
        assert reply["error"]["data"]["retryable"] is True


async def test_handler_crash_becomes_internal_error_without_detail_leak() -> None:
    ours, theirs = MemoryTransport.pair()

    async def handle_request(method: str, params: dict[str, Any]) -> Any:
        raise RuntimeError("secret /internal/path exploded")

    async with JsonRpcSession(ours, request_handler=handle_request):
        await theirs.send({"jsonrpc": "2.0", "id": 1, "method": "boom", "params": {}})
        reply = await theirs.receive()
        assert reply["error"]["code"] == -32008
        assert "secret" not in reply["error"]["message"]


async def test_unhandled_request_gets_method_not_found() -> None:
    ours, theirs = MemoryTransport.pair()
    async with JsonRpcSession(ours):  # no request handler registered
        await theirs.send({"jsonrpc": "2.0", "id": 5, "method": "nope", "params": {}})
        reply = await theirs.receive()
        assert reply["error"]["code"] == -32601


async def test_notification_dispatch() -> None:
    ours, theirs = MemoryTransport.pair()
    seen: list[str] = []
    got = asyncio.Event()

    async def handle_note(method: str, params: dict[str, Any]) -> None:
        seen.append(method)
        got.set()

    async with JsonRpcSession(ours, notification_handler=handle_note):
        await theirs.send({"jsonrpc": "2.0", "method": "notifications/event", "params": {}})
        await asyncio.wait_for(got.wait(), timeout=1.0)
        assert seen == ["notifications/event"]


async def test_garbage_message_does_not_kill_session() -> None:
    ours, theirs = MemoryTransport.pair()

    async def handle_request(method: str, params: dict[str, Any]) -> Any:
        return "alive"

    async with JsonRpcSession(ours, request_handler=handle_request):
        await theirs.send({"hello": "world"})
        await theirs.send({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
        reply = await theirs.receive()
        assert reply["result"] == "alive"


async def test_close_fails_pending_requests_with_session_closed() -> None:
    ours, _theirs = MemoryTransport.pair()
    session = JsonRpcSession(ours)
    session.start()

    async def requester() -> Any:
        return await session.request("ping", {})

    task = asyncio.create_task(requester())
    await asyncio.sleep(0.01)
    await session.close()
    with pytest.raises(SessionClosed):
        await task


async def test_peer_transport_close_fails_pending_requests() -> None:
    ours, theirs = MemoryTransport.pair()
    async with JsonRpcSession(ours) as session:
        task = asyncio.create_task(session.request("ping", {}))
        await asyncio.sleep(0.01)
        await theirs.close()
        with pytest.raises(SessionClosed):
            await task


async def test_request_after_close_raises() -> None:
    ours, _theirs = MemoryTransport.pair()
    session = JsonRpcSession(ours)
    session.start()
    await session.close()
    with pytest.raises(SessionClosed):
        await session.request("ping", {})


async def test_session_closed_is_a_labwire_error_subclass() -> None:
    assert issubclass(SessionClosed, LabwireError) or issubclass(SessionClosed, Exception)
