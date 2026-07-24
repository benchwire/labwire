"""Labwire transports: in-memory (tests) and WebSocket (SPEC §5).

Example:
    >>> from labwire.core.transport import MemoryTransport
    >>> a, b = MemoryTransport.pair()
"""

from labwire.core.transport.base import Transport, TransportClosed
from labwire.core.transport.memory import MemoryTransport
from labwire.core.transport.websocket import WebSocketTransport

__all__ = ["MemoryTransport", "Transport", "TransportClosed", "WebSocketTransport"]
