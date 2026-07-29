"""End-to-end: Labwire instruments exposed as MCP tools, both protocol eras.

Every session-level test runs twice: once as a 2026-07-28 client and once
as a legacy handshake-era client, against the SAME server object, which is
the honest test of the SDK's dual-era claim.
"""

import json
import os
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import pytest
from labwire.core import InstrumentServer
from labwire.drivers import SyringePump
from labwire.mcp.server import build_server, connect_instruments
from labwire.sim import SimSyringePump
from mcp import Client
from mcp.types import ElicitResult, TextContent

GRANT = "mcp-test-grant"

Era = Literal["2026-07-28", "legacy"]
ERAS: tuple[Era, ...] = ("2026-07-28", "legacy")


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
async def mcp_session(
    url: str,
    era: Era = "2026-07-28",
    *,
    s2_confirmation: str | None = None,
    elicitation_callback: Any = None,
) -> AsyncGenerator[Client]:
    # anyio cancel scopes must enter/exit in one task, so this cannot be a
    # pytest fixture: each test opens the session inside its own coroutine.
    instruments = await connect_instruments([url])
    try:
        server = build_server(instruments, s2_confirmation=s2_confirmation)
        async with Client(
            server,
            mode=era,
            elicitation_callback=elicitation_callback,
        ) as client:
            yield client
    finally:
        for instrument in instruments:
            await instrument.client.close()


def _text(content: list[Any]) -> str:
    parts = [c.text for c in content if isinstance(c, TextContent)]
    return "\n".join(parts)


@pytest.mark.parametrize("era", ERAS)
async def test_every_command_becomes_a_tool(pump_url: str, era: Era) -> None:
    async with mcp_session(pump_url, era) as session:
        tools = (await session.list_tools()).tools
        by_name = {t.name: t for t in tools}
        assert "SimPump-200__dispense" in by_name
        assert "SimPump-200__clear_occlusion" in by_name
        assert "SimPump-200__x-sim_inject_fault" in by_name  # sanitized: no slash
        dispense = by_name["SimPump-200__dispense"]
        assert dispense.input_schema["required"] == ["volume_ul", "rate_ul_min", "confirmation"]
        assert "confirmation" in dispense.input_schema["properties"]
        assert "S2" in (dispense.description or "")
        assert "uL/min" in (dispense.description or "")
        assert dispense.input_schema.get("additionalProperties") is False
        assert "SimPump-200" in (dispense.description or "")


@pytest.mark.parametrize("era", ERAS)
async def test_cancel_semantics_are_surfaced_per_tool(pump_url: str, era: Era) -> None:
    """v0.4's surfacing, now actually pinned: each tool states what cancel
    can physically do, so an agent plans around irreversibility."""
    async with mcp_session(pump_url, era) as session:
        tools = (await session.list_tools()).tools
        by_name = {t.name: t.description or "" for t in tools}
        assert "Cancel: abort." in by_name["SimPump-200__dispense"]
        assert "'unconfirmed'" in by_name["SimPump-200__dispense"]
        assert "Cancel: none." in by_name["SimPump-200__clear_occlusion"]
        assert "runs to completion" in by_name["SimPump-200__clear_occlusion"]


@pytest.mark.parametrize("era", ERAS)
async def test_tools_list_is_deterministic(pump_url: str, era: Era) -> None:
    """2026-07-28 SHOULD: deterministic ordering for client caching."""
    async with mcp_session(pump_url, era) as session:
        first = [t.name for t in (await session.list_tools()).tools]
        second = [t.name for t in (await session.list_tools()).tools]
        assert first == second


@pytest.mark.parametrize("era", ERAS)
async def test_calling_a_tool_runs_the_command(pump_url: str, era: Era) -> None:
    async with mcp_session(pump_url, era) as session:
        outcome = await session.call_tool(
            "SimPump-200__dispense",
            {"volume_ul": 60.0, "rate_ul_min": 60000.0, "confirmation": GRANT},
        )
        assert not outcome.is_error, _text(outcome.content)
        payload = json.loads(_text(outcome.content))
        assert payload["dispensed_ul"] == pytest.approx(60.0, rel=0.05)


@pytest.mark.parametrize("era", ERAS)
async def test_bad_params_surface_as_tool_error(pump_url: str, era: Era) -> None:
    async with mcp_session(pump_url, era) as session:
        outcome = await session.call_tool(
            "SimPump-200__dispense",
            {"volume_ul": "lots", "rate_ul_min": 1.0, "confirmation": GRANT},
        )
        assert outcome.is_error
        assert "validation" in _text(outcome.content)


@pytest.mark.parametrize("era", ERAS)
async def test_interlock_errors_are_readable(pump_url: str, era: Era) -> None:
    async with mcp_session(pump_url, era) as session:
        inject = await session.call_tool("SimPump-200__x-sim_inject_fault", {"kind": "occlusion"})
        assert not inject.is_error
        outcome = await session.call_tool(
            "SimPump-200__dispense",
            {"volume_ul": 500.0, "rate_ul_min": 6000.0, "confirmation": GRANT},
        )
        assert outcome.is_error
        assert "interlock" in _text(outcome.content)
        cleared = await session.call_tool("SimPump-200__clear_occlusion", {})
        assert not cleared.is_error


@pytest.mark.parametrize("era", ERAS)
async def test_unknown_tool_is_a_protocol_error(pump_url: str, era: Era) -> None:
    """v2 semantics: an unknown tool is -32602, not a swallowed tool error."""
    from mcp import MCPError

    async with mcp_session(pump_url, era) as session:
        with pytest.raises(MCPError, match="unknown tool"):
            await session.call_tool("nope", {})


async def test_stdio_console_entry_point(pump_url: str) -> None:
    """The installed entry point serves stdio and a v2 client connects."""
    from mcp import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "labwire.mcp", pump_url], env=dict(os.environ)
    )
    async with Client(stdio_client(params)) as session:
        tools = (await session.list_tools()).tools
        assert any(t.name == "SimPump-200__dispense" for t in tools)


# --- input-required: the human in the loop (2026-07-28 only) ----------------


async def test_s2_approval_is_surfaced_and_injects_the_confirmation(pump_url: str) -> None:
    """The client surfaces the S2 approval; the agent never sees the token."""
    seen: list[str] = []

    async def approve(context: Any, params: Any) -> ElicitResult:
        seen.append(params.message)
        return ElicitResult(action="accept", content={"approve": True})

    async with mcp_session(
        pump_url, "2026-07-28", s2_confirmation=GRANT, elicitation_callback=approve
    ) as session:
        outcome = await session.call_tool(
            "SimPump-200__dispense", {"volume_ul": 60.0, "rate_ul_min": 60000.0}
        )
        assert not outcome.is_error, _text(outcome.content)
        payload = json.loads(_text(outcome.content))
        assert payload["dispensed_ul"] == pytest.approx(60.0, rel=0.05)
    assert len(seen) == 1
    assert "dispense" in seen[0]
    assert "volume_ul" in seen[0]  # the human saw the exact parameters
    assert GRANT not in seen[0]  # and never the token


async def test_s2_decline_submits_nothing(pump_url: str) -> None:
    async def decline(context: Any, params: Any) -> ElicitResult:
        return ElicitResult(action="decline")

    async with mcp_session(
        pump_url, "2026-07-28", s2_confirmation=GRANT, elicitation_callback=decline
    ) as session:
        outcome = await session.call_tool(
            "SimPump-200__dispense", {"volume_ul": 60.0, "rate_ul_min": 60000.0}
        )
        assert outcome.is_error
        assert "declined" in _text(outcome.content)
        assert "nothing was submitted" in _text(outcome.content)


async def test_legacy_era_keeps_the_parameter_path(pump_url: str) -> None:
    """A legacy client with no confirmation gets the honest refusal, not a
    dangling elicitation it cannot answer."""
    async with mcp_session(pump_url, "legacy", s2_confirmation=GRANT) as session:
        outcome = await session.call_tool(
            "SimPump-200__dispense", {"volume_ul": 60.0, "rate_ul_min": 60000.0}
        )
        assert outcome.is_error
        assert "confirmation" in _text(outcome.content)


# --- tasks extension (io.modelcontextprotocol/tasks), 2026-07-28 only --------


from typing import Literal  # noqa: E402


class _TaskWire(  # what the CLIENT sees in a claimed "task" result
    __import__("mcp_types").Result
):
    result_type: Literal["task"] = "task"
    task_id: str
    status: str
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None
    status_message: str | None = None


def _tasks_client_extension(
    observed: dict[str, Any], *, cancel_after_poll: bool = False, probe_bogus: bool = False
) -> Any:
    from mcp import MCPError
    from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim
    from mcp.types import CallToolResult as ClientCallToolResult
    from mcp_types import Request

    class LabwireTasks(ClientExtension):
        identifier = "io.modelcontextprotocol/tasks"

        def claims(self) -> Any:
            return [ResultClaim(result_type="task", model=_TaskWire, resolve=self._resolve)]

        async def _resolve(self, claimed: _TaskWire, ctx: ClaimContext) -> ClientCallToolResult:
            observed["task_id"] = claimed.task_id
            observed["initial_status"] = claimed.status
            if probe_bogus:
                try:
                    await ctx.session.send_request(
                        Request(method="tasks/get", params=_ClientTaskParams(task_id="task-nope")),
                        _TaskDetail,
                    )
                except MCPError as exc:
                    observed["bogus_error"] = str(exc)
            polls = 0
            while True:
                detail = await ctx.session.send_request(
                    Request(method="tasks/get", params=_ClientTaskParams(task_id=claimed.task_id)),
                    _TaskDetail,
                )
                observed.setdefault("statuses", []).append(detail.status)
                polls += 1
                if cancel_after_poll and polls == 1:
                    await ctx.session.send_request(
                        Request(
                            method="tasks/cancel", params=_ClientTaskParams(task_id=claimed.task_id)
                        ),
                        __import__("mcp_types").Result,
                    )
                    observed["cancel_acked"] = True
                if detail.status in ("completed", "failed", "cancelled"):
                    observed["final"] = detail.model_dump(mode="json", by_alias=True)
                    if detail.status == "completed" and detail.result is not None:
                        return ClientCallToolResult.model_validate(detail.result)
                    return ClientCallToolResult(content=[], is_error=detail.status == "failed")
                await asyncio.sleep((detail.poll_interval_ms or 100) / 1000.0)

    return LabwireTasks()


class _ClientTaskParams(__import__("mcp_types").RequestParams):
    task_id: str


class _TaskDetail(__import__("mcp_types").Result):
    result_type: str = "complete"
    task_id: str
    status: str
    status_message: str | None = None
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


import asyncio  # noqa: E402


@asynccontextmanager
async def tasks_session(url: str, extension: Any) -> AsyncGenerator[Client]:
    instruments = await connect_instruments([url])
    try:
        server = build_server(instruments)
        async with Client(server, mode="2026-07-28", extensions=[extension]) as client:
            yield client
    finally:
        for instrument in instruments:
            await instrument.client.close()


async def test_long_command_becomes_a_task_and_completes(pump_url: str) -> None:
    """dispense (estimated 60 s) task-wraps for a declaring client; the
    resolver polls tasks/get and the final result carries the run."""
    observed: dict[str, Any] = {}
    async with tasks_session(pump_url, _tasks_client_extension(observed)) as session:
        outcome = await session.call_tool(
            "SimPump-200__dispense",
            {"volume_ul": 60.0, "rate_ul_min": 60000.0, "confirmation": GRANT},
        )
    assert observed["task_id"].startswith("task-")
    assert observed["initial_status"] == "working"
    assert observed["final"]["status"] == "completed"
    assert not outcome.is_error
    payload = json.loads(_text(outcome.content))
    assert payload["result"]["dispensed_ul"] == pytest.approx(60.0, rel=0.05)
    assert "command_id" in payload  # the signed bundle reference rides along


async def test_task_cancel_on_abort_command_settles_honestly(pump_url: str) -> None:
    """tasks/cancel on the abort-declared dispense really cancels: the task
    ends cancelled with the settlement summarized (a cancelled task cannot
    carry a result, so the statusMessage names the outcome and bundle)."""
    observed: dict[str, Any] = {}
    async with tasks_session(
        pump_url, _tasks_client_extension(observed, cancel_after_poll=True)
    ) as session:
        outcome = await session.call_tool(
            "SimPump-200__dispense",
            {"volume_ul": 50000.0, "rate_ul_min": 6000.0, "confirmation": GRANT},
        )
    assert observed["cancel_acked"] is True
    final = observed["final"]
    assert final["status"] in ("cancelled", "completed")  # cancel can lose the race
    if final["status"] == "cancelled":
        assert "settlement" in (final.get("statusMessage") or "")
        assert outcome.content == []
    del outcome


async def test_non_declaring_client_never_sees_a_task(pump_url: str) -> None:
    """Spec MUST: no CreateTaskResult without the per-request capability."""
    async with mcp_session(pump_url, "2026-07-28") as session:
        outcome = await session.call_tool(
            "SimPump-200__dispense",
            {"volume_ul": 60.0, "rate_ul_min": 60000.0, "confirmation": GRANT},
        )
        assert not outcome.is_error
        payload = json.loads(_text(outcome.content))
        assert "dispensed_ul" in payload  # a plain CallToolResult, no task


async def test_unknown_task_id_is_invalid_params(pump_url: str) -> None:
    """Spec: unknown/expired taskId on tasks/get is -32602."""
    observed: dict[str, Any] = {}
    async with tasks_session(
        pump_url, _tasks_client_extension(observed, probe_bogus=True)
    ) as session:
        await session.call_tool(
            "SimPump-200__dispense",
            {"volume_ul": 60.0, "rate_ul_min": 60000.0, "confirmation": GRANT},
        )
    assert "unknown or expired task" in observed["bogus_error"]
