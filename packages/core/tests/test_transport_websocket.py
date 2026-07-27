"""Tests for the WebSocket transport over a real localhost socket."""

import asyncio
from typing import Any

import pytest
import websockets.asyncio.server
from labwire.core.transport import TransportClosed, WebSocketTransport


async def _echo_server() -> tuple[websockets.asyncio.server.Server, int]:
    async def handler(conn: websockets.asyncio.server.ServerConnection) -> None:
        async for frame in conn:
            await conn.send(frame)

    server = await websockets.asyncio.server.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_connect_send_receive_round_trip() -> None:
    server, port = await _echo_server()
    try:
        transport = await WebSocketTransport.connect(f"ws://127.0.0.1:{port}")
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
        await transport.send(message)
        assert await transport.receive() == message
        await transport.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_receive_raises_transport_closed_when_server_drops() -> None:
    async def handler(conn: websockets.asyncio.server.ServerConnection) -> None:
        await conn.close()

    server = await websockets.asyncio.server.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        transport = await WebSocketTransport.connect(f"ws://127.0.0.1:{port}")
        with pytest.raises(TransportClosed):
            await transport.receive()
    finally:
        server.close()
        await server.wait_closed()


async def test_binary_frames_are_ignored_per_spec() -> None:
    async def handler(conn: websockets.asyncio.server.ServerConnection) -> None:
        await conn.send(b"\x00\x01binary-to-ignore")
        await conn.send('{"jsonrpc": "2.0", "id": 1, "result": {}}')
        await asyncio.sleep(0.2)

    server = await websockets.asyncio.server.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        transport = await WebSocketTransport.connect(f"ws://127.0.0.1:{port}")
        received = await transport.receive()
        assert received == {"jsonrpc": "2.0", "id": 1, "result": {}}
        await transport.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_send_after_close_raises() -> None:
    server, port = await _echo_server()
    try:
        transport = await WebSocketTransport.connect(f"ws://127.0.0.1:{port}")
        await transport.close()
        with pytest.raises(TransportClosed):
            await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    finally:
        server.close()
        await server.wait_closed()


async def test_unparseable_frame_gets_parse_error_and_session_survives() -> None:
    """SPEC 12: garbage in one frame answers -32700 without killing the link."""
    server, port = await _echo_server()
    try:
        transport = await WebSocketTransport.connect(f"ws://127.0.0.1:{port}")
        # The echo server reflects whatever we send, so send garbage to
        # ourselves: OUR transport must answer it with -32700 (which the echo
        # then reflects back for us to observe) and keep the session alive.
        await transport._connection.send("this is not json {")  # pyright: ignore[reportPrivateUsage]
        answer = await transport.receive()
        assert answer["error"]["code"] == -32700
        assert answer["id"] is None
        good: dict[str, Any] = {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
        await transport.send(good)
        assert await transport.receive() == good
        await transport.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_non_object_json_gets_invalid_request() -> None:
    """SPEC 12: a JSON array or scalar frame answers -32600, id null."""
    server, port = await _echo_server()
    try:
        transport = await WebSocketTransport.connect(f"ws://127.0.0.1:{port}")
        await transport._connection.send("[1, 2, 3]")  # pyright: ignore[reportPrivateUsage]
        answer = await transport.receive()
        assert answer["error"]["code"] == -32600
        assert answer["id"] is None
        await transport.close()
    finally:
        server.close()
        await server.wait_closed()
