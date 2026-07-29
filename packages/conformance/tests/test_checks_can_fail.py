"""Negative proof: against a deliberately broken server, checks FAIL.

A suite whose checks cannot fail proves nothing. This server speaks just
enough JSON-RPC to look alive and then violates the safety and taxonomy
requirements on purpose.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from labwire.conformance import Report, RunOptions, Status, run_suite
from websockets.asyncio.server import ServerConnection, serve

_DESCRIPTOR: dict[str, Any] = {
    "identity": {
        "manufacturer": "Broken Instruments Inc",
        "model": "Broken-1",
        "serial_number": "BRK-1",
        "firmware_version": "0.0.1",
    },
    "commands": [
        {
            "name": "vent",
            "title": "Vent",
            "description": "Vents something irreversibly.",
            "params_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "safety_class": "S2",
            "interruptible": False,
        }
    ],
    "channels": [],
    "events": [],
    "interlocks": [],
    "resources": [],
}


async def _handle(connection: ServerConnection) -> None:
    async for raw in connection:
        try:
            message = json.loads(raw)
        except ValueError:
            # Violation: a parse error just closes the connection.
            await connection.close()
            return
        if message.get("method") == "notifications/initialized":
            continue
        request_id = message.get("id")
        method = message.get("method")
        if method == "initialize":
            result: Any = {
                "protocol_version": "0.4",
                "server_info": {"name": "broken", "version": "0"},
                "capabilities": {},
            }
        elif method == "instrument/describe":
            result = _DESCRIPTOR
        elif method == "ping":
            result = {}
        elif method == "command/submit":
            # Violation: an S2 command accepted without confirmation.
            result = {"command_id": "brk-1", "status": "succeeded"}
        else:
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        # Violation: unknown methods get a made-up code.
                        "error": {"code": -1, "message": "whatever"},
                    }
                )
            )
            continue
        await connection.send(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}))


@pytest.fixture
async def broken_url() -> AsyncIterator[str]:
    async with serve(_handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


def _by_id(report: Report) -> dict[str, Status]:
    return {o.check_id: o.status for o in report.outcomes}


async def test_broken_server_fails_the_right_checks(broken_url: str) -> None:
    report = await asyncio.wait_for(run_suite(broken_url, RunOptions()), timeout=30)
    statuses = _by_id(report)
    assert statuses["safety.s2.refused_unconfirmed"] is Status.FAILED
    assert statuses["core.jsonrpc.method_not_found"] is Status.FAILED
    assert statuses["core.jsonrpc.parse_error_recovery"] is Status.FAILED
    assert statuses["core.errors.unsupported_command"] is Status.FAILED
    level, _ = report.verdict()
    assert level == "none"


async def test_broken_server_still_passes_what_it_does_right(broken_url: str) -> None:
    """Failing everything would be as suspicious as passing everything."""
    report = await asyncio.wait_for(run_suite(broken_url, RunOptions()), timeout=30)
    statuses = _by_id(report)
    assert statuses["core.initialize.negotiates_0_4"] is Status.PASSED
    assert statuses["core.describe.descriptor_valid"] is Status.PASSED
    assert statuses["core.ping"] is Status.PASSED
