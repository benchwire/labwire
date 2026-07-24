"""Transport abstraction: framed JSON-RPC message movers (SPEC §5).

Transports move parsed JSON objects; framing is each transport's job, so the
session layer stays transport-blind.

Example:
    >>> from labwire.core.transport import MemoryTransport, Transport
    >>> a, b = MemoryTransport.pair()
    >>> isinstance(a, Transport)
    True
"""

from typing import Any, Protocol, runtime_checkable


class TransportClosed(Exception):
    """Raised on receive at end-of-stream and on use of a closed transport."""


@runtime_checkable
class Transport(Protocol):
    """One end of a bidirectional, message-framed connection.

    Example:
        >>> async def echo_once(t: Transport) -> None:
        ...     await t.send(await t.receive())
    """

    async def send(self, message: dict[str, Any]) -> None:
        """Send one JSON-RPC message object."""
        ...

    async def receive(self) -> dict[str, Any]:
        """Receive the next message object; raises TransportClosed at EOF."""
        ...

    async def close(self) -> None:
        """Close the transport; idempotent."""
        ...
