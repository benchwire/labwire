"""Shared line-protocol TCP client used by the serial-style drivers."""

import asyncio


class LineProtocolClient:
    """One command/one reply line protocol over TCP, serialized by a lock.

    Example:
        >>> # link = LineProtocolClient("127.0.0.1", 4001)
        >>> # await link.open(); reply = await link.command("STAT?")
    """

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        """Connect to the device."""
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)

    async def close(self) -> None:
        """Close the connection."""
        if self._writer is not None:
            self._writer.close()
            self._reader = None
            self._writer = None

    async def command(self, line: str) -> str:
        """Send one command line and return the stripped reply line."""
        if self._reader is None or self._writer is None:
            raise ConnectionError("not connected")
        async with self._lock:
            self._writer.write((line + "\r\n").encode())
            await self._writer.drain()
            reply = await self._reader.readline()
        if not reply:
            raise ConnectionError("device closed the connection")
        return reply.decode().strip()
