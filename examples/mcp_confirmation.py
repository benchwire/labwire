"""The 2026-07-28 human-in-the-loop confirmation, end to end.

An MCP client (the role Claude Desktop or any MCP host plays) calls an S2
tool WITHOUT a confirmation. Instead of failing, the adapter answers
input_required with an elicitation; the CLIENT surfaces it to the human;
on approval the adapter injects the deployment's standing confirmation
and runs the command. The agent never handles the token.

The honest caveat, stated where it belongs: this flow proves a human at
the client approved THIS call with THESE parameters; like the legacy
parameter path, it does not prove WHO (identity_verified stays false in
the signed record). The legacy path remains for handshake-era clients
and proves intent only.

Run it (zero hardware):

    uv run examples/mcp_confirmation.py
"""

import asyncio
from typing import Any

from labwire.core import InstrumentServer
from labwire.drivers import SyringePump
from labwire.mcp.server import build_server, connect_instruments
from labwire.sim import SimSyringePump
from mcp import Client
from mcp.types import ElicitResult

CONFIRMATION = "operator-standing-confirmation-example"


async def surface_to_human(context: Any, params: Any) -> ElicitResult:
    """What an MCP host does with an elicitation: show it, ask, answer."""
    print("\n  +--- the CLIENT surfaces this to the human " + "-" * 20)
    for line in str(params.message).split(". "):
        print(f"  | {line.strip().rstrip('.')}.")
    print("  +" + "-" * 62)
    print("  [human] approve  (scripted for the demo; a real host shows a dialog)")
    return ElicitResult(action="accept", content={"approve": True})


async def main() -> None:
    """Serve a pump, adapt it to MCP, and drive the approval round trip."""
    sim = SimSyringePump(seed=11)
    await sim.start()
    server = InstrumentServer(SyringePump("127.0.0.1", sim.port), confirmation_token=CONFIRMATION)
    async with server.serve_websocket("127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        instruments = await connect_instruments([f"ws://127.0.0.1:{port}"])
        try:
            adapter = build_server(instruments, s2_confirmation=CONFIRMATION)
            print("MCP client (protocol 2026-07-28) calling S2 tool with NO confirmation")
            async with Client(
                adapter, mode="2026-07-28", elicitation_callback=surface_to_human
            ) as client:
                outcome = await client.call_tool(
                    "SimPump-200__dispense", {"volume_ul": 80.0, "rate_ul_min": 60000.0}
                )
                text = "".join(getattr(block, "text", "") for block in outcome.content)
                print(f"\n  result (after approval): {text}")
                print("  the model supplied no confirmation; the human's approval did")
        finally:
            for instrument in instruments:
                await instrument.client.close()
    await server.aclose()
    await sim.stop()
    print("\ndone")


if __name__ == "__main__":
    asyncio.run(main())
