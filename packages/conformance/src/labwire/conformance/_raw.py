"""A deliberately independent JSON-RPC-over-WebSocket probe.

The runner checks most behavior through the reference client, but the
handshake and malformed-input checks must not depend on a client that
already implements the spec correctly, so this speaks raw frames.
"""

import json
from types import TracebackType
from typing import Any

from websockets.asyncio.client import ClientConnection, connect


class RawWire:
    """Raw frames to a Labwire WebSocket endpoint.

    Example:
        >>> # async with RawWire("ws://127.0.0.1:9500") as wire:
        >>> #     reply = await wire.call("ping", {}, request_id=1)
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._ws: ClientConnection | None = None

    async def __aenter__(self) -> "RawWire":
        self._ws = await connect(self._url)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def send_text(self, text: str) -> None:
        """Send one raw text frame, valid JSON or not."""
        assert self._ws is not None
        await self._ws.send(text)

    async def recv_json(self, timeout: float = 5.0) -> Any:
        """Receive one frame and parse it as JSON."""
        import asyncio

        assert self._ws is not None
        raw = await asyncio.wait_for(self._ws.recv(), timeout)
        return json.loads(raw)

    async def call(self, method: str, params: Any, request_id: int) -> Any:
        """Send one request and return the matching response object.

        Skips server-initiated notifications (no ``id``) until the response
        with ``request_id`` arrives.
        """
        await self.send_text(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        )
        while True:
            message = await self.recv_json()
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

    async def initialize(self, request_id: int = 1) -> Any:
        """Run the client half of the SPEC 6 handshake and return the result."""
        response = await self.call(
            "initialize",
            {
                "protocol_version": "0.3",
                "client_info": {"name": "labwire-conformance", "version": "0.3.0"},
                "capabilities": {},
            },
            request_id,
        )
        await self.send_text(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        )
        return response
