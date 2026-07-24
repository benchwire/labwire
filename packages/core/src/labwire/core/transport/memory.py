"""In-memory transport pair for tests and in-process wiring.

Example:
    >>> import asyncio
    >>> from labwire.core.transport import MemoryTransport
    >>> async def demo() -> dict[str, object]:
    ...     a, b = MemoryTransport.pair()
    ...     await a.send({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    ...     return await b.receive()
    >>> asyncio.run(demo())["method"]
    'ping'
"""

import asyncio
from typing import Any

from labwire.core.transport.base import TransportClosed

_SENTINEL: dict[str, Any] = {}


class MemoryTransport:
    """One end of a queue-backed in-process transport pair."""

    def __init__(
        self,
        inbox: asyncio.Queue[dict[str, Any]],
        outbox: asyncio.Queue[dict[str, Any]],
        closed: asyncio.Event,
    ) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._closed = closed

    @classmethod
    def pair(cls) -> tuple["MemoryTransport", "MemoryTransport"]:
        """Create two connected ends sharing a closed flag.

        Example:
            >>> a, b = MemoryTransport.pair()
        """
        q_ab: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        q_ba: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        closed = asyncio.Event()
        return cls(q_ba, q_ab, closed), cls(q_ab, q_ba, closed)

    async def send(self, message: dict[str, Any]) -> None:
        """Send one message to the peer; raises TransportClosed if closed."""
        if self._closed.is_set():
            raise TransportClosed("memory transport is closed")
        await self._outbox.put(message)

    async def receive(self) -> dict[str, Any]:
        """Receive the next message; drains queued messages before raising."""
        if self._closed.is_set() and self._inbox.empty():
            raise TransportClosed("memory transport is closed")
        message = await self._inbox.get()
        if message is _SENTINEL:
            await self._inbox.put(_SENTINEL)
            raise TransportClosed("memory transport is closed")
        return message

    async def close(self) -> None:
        """Close both ends; pending and future receives raise TransportClosed."""
        if self._closed.is_set():
            return
        self._closed.set()
        await self._inbox.put(_SENTINEL)
        await self._outbox.put(_SENTINEL)
