"""Serve a simulated syringe pump on ws://127.0.0.1:9520 until Ctrl-C.

The companion to the MCP section of the README: run this in one terminal,
then point `labwire-mcp ws://127.0.0.1:9520` (or any Labwire client) at it.

    uv run examples/serve_pump.py
"""

import asyncio
import contextlib

from labwire.core import InstrumentServer
from labwire.drivers import SyringePump
from labwire.sim import SimSyringePump

PORT = 9520


async def main() -> None:
    """Start the sim and its instrument server; serve until interrupted."""
    sim = SimSyringePump(seed=1)
    await sim.start()
    server = InstrumentServer(SyringePump("127.0.0.1", sim.port))
    async with server.serve_websocket("127.0.0.1", PORT):
        print(f"SimPump-200 serving on ws://127.0.0.1:{PORT}  (Ctrl-C to stop)", flush=True)
        try:
            await asyncio.Future()
        finally:
            await server.aclose()
            await sim.stop()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
