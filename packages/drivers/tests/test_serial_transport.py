"""The serial link, tested against a PTY-backed line responder.

No hardware: the far end is a pseudo-terminal simulating an instrument
that answers one line per line, which is exactly the honest claim the
serial transport ships with ("real transports, tested against simulators,
awaiting hardware").
"""

import asyncio
import os
import pty
import sys

import pytest
from labwire.drivers._lineproto import LineProtocolClient

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PTY responder is POSIX-only")


class PtyInstrument:
    """A line-protocol instrument on the master side of a PTY."""

    def __init__(self) -> None:
        self.master_fd, slave_fd = pty.openpty()
        self.device = os.ttyname(slave_fd)
        self._buffer = b""
        self._slave_fd = slave_fd

    def start(self) -> None:
        """Answer complete lines as they arrive."""
        asyncio.get_running_loop().add_reader(self.master_fd, self._on_readable)

    def _on_readable(self) -> None:
        self._buffer += os.read(self.master_fd, 1024)
        while b"\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition(b"\n")
            command = line.strip().decode()
            if command == "*IDN?":
                reply = "PTY Instruments,PTY-1,SN-000,0.0.1"
            elif command.startswith("ECHO "):
                reply = command.removeprefix("ECHO ")
            else:
                reply = "ERR unknown"
            os.write(self.master_fd, (reply + "\r\n").encode())

    def stop(self) -> None:
        """Detach and close both ends."""
        asyncio.get_running_loop().remove_reader(self.master_fd)
        os.close(self.master_fd)
        os.close(self._slave_fd)


async def test_serial_link_round_trips_lines() -> None:
    instrument = PtyInstrument()
    instrument.start()
    link = LineProtocolClient.serial(instrument.device, baudrate=9600)
    try:
        await link.open()
        assert await link.command("*IDN?") == "PTY Instruments,PTY-1,SN-000,0.0.1"
        assert await link.command("ECHO hello") == "hello"
    finally:
        await link.close()
        instrument.stop()


async def test_serial_link_serializes_concurrent_commands() -> None:
    """Two overlapping commands never interleave on the wire."""
    instrument = PtyInstrument()
    instrument.start()
    link = LineProtocolClient.serial(instrument.device)
    try:
        await link.open()
        replies = await asyncio.gather(link.command("ECHO one"), link.command("ECHO two"))
        assert sorted(replies) == ["one", "two"]
    finally:
        await link.close()
        instrument.stop()


async def test_commanding_before_open_names_the_endpoint() -> None:
    link = LineProtocolClient.serial("/dev/does-not-exist")
    with pytest.raises(ConnectionError, match="serial"):
        await link.command("*IDN?")
