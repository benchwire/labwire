"""End-to-end: Labwire instruments exposed as MCP tools."""

import json
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from labwire.core import InstrumentServer
from labwire.drivers import SyringePump
from labwire.mcp.server import build_server, connect_instruments
from labwire.sim import SimSyringePump
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent

GRANT = "mcp-test-grant"


@pytest.fixture
async def pump_url() -> AsyncIterator[str]:
    sim = SimSyringePump(seed=5)
    await sim.start()
    # dispense is S2, so the adapter must surface and forward a confirmation
    server = InstrumentServer(SyringePump("127.0.0.1", sim.port), confirmation_token=GRANT)
    async with server.serve_websocket("127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
    await server.aclose()
    await sim.stop()


@asynccontextmanager
async def mcp_session(url: str) -> AsyncGenerator[ClientSession]:
    # anyio cancel scopes must enter/exit in one task, so this cannot be a
    # pytest fixture: each test opens the session inside its own coroutine.
    instruments = await connect_instruments([url])
    try:
        server = build_server(instruments)
        async with create_connected_server_and_client_session(server) as session:
            yield session
    finally:
        for instrument in instruments:
            await instrument.client.close()


def _text(content: list[Any]) -> str:
    parts = [c.text for c in content if isinstance(c, TextContent)]
    return "\n".join(parts)


async def test_every_command_becomes_a_tool(pump_url: str) -> None:
    async with mcp_session(pump_url) as session:
        tools = (await session.list_tools()).tools
        by_name = {t.name: t for t in tools}
        assert "SimPump-200__dispense" in by_name
        assert "SimPump-200__clear_occlusion" in by_name
        assert "SimPump-200__x-sim_inject_fault" in by_name  # sanitized: no slash
        dispense = by_name["SimPump-200__dispense"]
        assert dispense.inputSchema["required"] == ["volume_ul", "rate_ul_min", "confirmation"]
        assert "confirmation" in dispense.inputSchema["properties"]
        assert "S2" in (dispense.description or "")
        assert "uL/min" in (dispense.description or "")
        assert dispense.inputSchema.get("additionalProperties") is False
        assert dispense.description is not None
        assert "uL/min" in dispense.description  # units surfaced to the agent
        assert "SimPump-200" in dispense.description


async def test_calling_a_tool_runs_the_command(pump_url: str) -> None:
    async with mcp_session(pump_url) as session:
        outcome = await session.call_tool(
            "SimPump-200__dispense",
            {"volume_ul": 60.0, "rate_ul_min": 60000.0, "confirmation": GRANT},
        )
        assert not outcome.isError, _text(outcome.content)
        payload = json.loads(_text(outcome.content))
        assert payload["dispensed_ul"] == pytest.approx(60.0, rel=0.05)


async def test_bad_params_surface_as_tool_error(pump_url: str) -> None:
    async with mcp_session(pump_url) as session:
        outcome = await session.call_tool(
            "SimPump-200__dispense",
            {"volume_ul": "lots", "rate_ul_min": 1.0, "confirmation": GRANT},
        )
        assert outcome.isError
        assert "validation" in _text(outcome.content)


async def test_interlock_errors_are_readable(pump_url: str) -> None:
    async with mcp_session(pump_url) as session:
        inject = await session.call_tool("SimPump-200__x-sim_inject_fault", {"kind": "occlusion"})
        assert not inject.isError
        outcome = await session.call_tool(
            "SimPump-200__dispense",
            {"volume_ul": 500.0, "rate_ul_min": 6000.0, "confirmation": GRANT},
        )
        assert outcome.isError
        assert "interlock" in _text(outcome.content)
        cleared = await session.call_tool("SimPump-200__clear_occlusion", {})
        assert not cleared.isError


async def test_unknown_tool_is_an_error(pump_url: str) -> None:
    async with mcp_session(pump_url) as session:
        outcome = await session.call_tool("nope", {})
        assert outcome.isError


async def test_stdio_console_entry_point(pump_url: str) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "labwire.mcp", pump_url])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        assert any(t.name == "SimPump-200__dispense" for t in tools)
