"""Labwire MCP adapter: drive instruments from Claude and other MCP clients.

Example:
    >>> from labwire.mcp.server import build_server, connect_instruments
"""

from labwire.mcp.server import ConnectedInstrument, build_server, connect_instruments

__all__ = ["ConnectedInstrument", "build_server", "connect_instruments"]
