"""WebSocket transport (SPEC §5.1): one JSON-RPC message per text frame.

Example:
    >>> # transport = await WebSocketTransport.connect("ws://127.0.0.1:9520")
    >>> # await transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
"""

import asyncio
import json
from typing import Any

from labwire.core.transport.base import TransportClosed
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed


class WebSocketTransport:
    """A :class:`~labwire.core.transport.Transport` over one WebSocket connection.

    Send order is preserved via an internal lock, which SPEC §8.2's
    response-before-notifications ordering relies on. Binary frames are
    ignored per SPEC §5.1.

    Example:
        >>> # transport = await WebSocketTransport.connect("ws://127.0.0.1:9520")
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._send_lock = asyncio.Lock()

    @classmethod
    async def connect(cls, url: str) -> "WebSocketTransport":
        """Open a client connection to a Labwire WebSocket server.

        Example:
            >>> # transport = await WebSocketTransport.connect("ws://127.0.0.1:9520")
        """
        return cls(await ws_connect(url))

    async def send(self, message: dict[str, Any]) -> None:
        """Send one message as one text frame; raises TransportClosed if gone."""
        try:
            async with self._send_lock:
                await self._connection.send(json.dumps(message))
        except ConnectionClosed as exc:
            raise TransportClosed("websocket connection closed") from exc

    async def receive(self) -> dict[str, Any]:
        """Receive the next text frame as a message; binary frames are skipped."""
        while True:
            try:
                frame = await self._connection.recv()
            except ConnectionClosed as exc:
                raise TransportClosed("websocket connection closed") from exc
            if isinstance(frame, bytes):
                continue  # binary frames are reserved and ignored (SPEC §5.1)
            try:
                parsed: Any = json.loads(frame)
            except json.JSONDecodeError:
                continue  # unparseable frame: skip; the session must survive
            if isinstance(parsed, dict):
                return parsed  # pyright: ignore[reportUnknownVariableType]
            # non-object payloads are not JSON-RPC 2.0 messages; skip them

    async def close(self) -> None:
        """Close the WebSocket connection; idempotent."""
        await self._connection.close()
