"""Console entry point: serve connected instruments as MCP tools over stdio.

Example:
    >>> # $ labwire-mcp ws://127.0.0.1:9520 ws://127.0.0.1:9521
"""

import argparse
import asyncio
import os
import sys

from labwire.mcp.server import build_server, connect_instruments

from mcp.server.stdio import stdio_server


async def _serve(urls: list[str]) -> None:
    # The S2 standing confirmation comes from the ENVIRONMENT, never a CLI
    # flag: flags are visible in process listings, environments are not.
    # Without it, S2 calls keep the legacy parameter path only.
    confirmation = os.environ.get("LABWIRE_MCP_CONFIRMATION")
    instruments = await connect_instruments(urls)
    try:
        server = build_server(instruments, s2_confirmation=confirmation)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        for instrument in instruments:
            await instrument.client.close()


def main() -> None:
    """Parse instrument URLs and serve MCP over stdio.

    Example:
        >>> # labwire-mcp ws://127.0.0.1:9520
    """
    parser = argparse.ArgumentParser(
        prog="labwire-mcp",
        description="Expose Labwire instruments as MCP tools over stdio.",
    )
    parser.add_argument(
        "urls",
        nargs="+",
        metavar="WS_URL",
        help="WebSocket URL(s) of running Labwire Instrument Servers",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_serve(args.urls))
    except KeyboardInterrupt:
        sys.exit(0)
