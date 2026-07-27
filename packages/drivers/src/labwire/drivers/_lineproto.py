"""Shared line-protocol client used by the serial-style drivers.

One command line out, one reply line back, serialized by a lock. The link
layer underneath is pluggable: TCP (always available) or a USB-serial
device (install the ``labwire-drivers[serial]`` extra). Both yield the
same asyncio stream pair, so every driver works over either unchanged.

Serial support uses ``pyserial-asyncio-fast`` (BSD-3-Clause, maintained by
the Home Assistant org as the successor of the dormant pyserial-asyncio,
whose transport blocks the event loop). It is an optional dependency and
is never vendored. Its asyncio integration is POSIX-oriented; on Windows
prefer TCP (most bench instruments with USB-serial also expose it via a
vendor VISA/TCP gateway). TODO-VERIFY: pyserial-asyncio-fast 0.16
behavior on Windows; never tested here.
"""

import asyncio
from collections.abc import Awaitable, Callable

_StreamPair = tuple[asyncio.StreamReader, asyncio.StreamWriter]


class LineProtocolClient:
    """One command/one reply line protocol over TCP or serial.

    Example:
        >>> # link = LineProtocolClient("127.0.0.1", 4001)          # TCP
        >>> # link = LineProtocolClient.serial("/dev/tty.usbserial")  # serial
        >>> # await link.open(); reply = await link.command("STAT?")
    """

    def __init__(self, host: str, port: int) -> None:
        self._describe = f"tcp://{host}:{port}"
        self._connect: Callable[[], Awaitable[_StreamPair]] = lambda: asyncio.open_connection(
            host, port
        )
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def tcp(cls, host: str, port: int) -> "LineProtocolClient":
        """A TCP link; identical to the plain constructor, named for symmetry."""
        return cls(host, port)

    @classmethod
    def serial(cls, device: str, baudrate: int = 9600) -> "LineProtocolClient":
        """A USB-serial link (requires the ``labwire-drivers[serial]`` extra).

        ``device`` is the serial device path (``/dev/tty.usbserial-XXXX``,
        ``/dev/ttyUSB0``); 8N1 framing, which is what SCPI-over-serial
        instruments overwhelmingly speak. Real hardware has NOT been tested
        against this transport; see docs/HARDWARE.md for the honest status.
        """
        try:
            import serial_asyncio_fast
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "serial support needs the optional extra: pip install 'labwire-drivers[serial]'"
            ) from exc

        client = cls.__new__(cls)
        client._describe = f"serial://{device}?baudrate={baudrate}"
        client._connect = lambda: serial_asyncio_fast.open_serial_connection(
            url=device, baudrate=baudrate
        )
        client._reader = None
        client._writer = None
        client._lock = asyncio.Lock()
        return client

    async def open(self) -> None:
        """Connect to the device."""
        self._reader, self._writer = await self._connect()

    async def close(self) -> None:
        """Close the connection."""
        if self._writer is not None:
            self._writer.close()
            self._reader = None
            self._writer = None

    async def command(self, line: str) -> str:
        """Send one command line and return the stripped reply line."""
        if self._reader is None or self._writer is None:
            raise ConnectionError(f"not connected ({self._describe})")
        async with self._lock:
            self._writer.write((line + "\r\n").encode())
            await self._writer.drain()
            reply = await self._reader.readline()
        if not reply:
            raise ConnectionError("device closed the connection")
        return reply.decode().strip()
