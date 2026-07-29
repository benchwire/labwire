"""Wire-level tests for InstrumentServer: lifecycle, commands, cancel, busy."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import pytest
from labwire.core.capabilities import IdentityInfo
from labwire.core.errors import BusyError, NotCancelableError, UnsupportedError, ValidationError
from labwire.core.server import CommandContext, Instrument, InstrumentServer, command
from labwire.core.session import JsonRpcSession
from labwire.core.transport import MemoryTransport
from pydantic import ConfigDict


class Sum(TypedDict):
    """A closed result schema for the test instrument."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]

    sum: float


class FakeClock:
    """Deterministic clock: sleeps yield control but take no wall time."""

    def __init__(self) -> None:
        self._now = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class Rig(Instrument):
    """Test instrument with fast, slow, and failing commands."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="TestRig-1",
        serial_number="SIM-0000",
        firmware_version="0.1.0",
    )

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    @command(units={"a": "1", "b": "1"}, returns_units={"sum": "1"})
    async def add(self, ctx: CommandContext, a: float, b: float) -> Sum:
        """Add two numbers instantly."""
        return {"sum": a + b}

    @command(cancel="abort")
    async def wait_for_release(self, ctx: CommandContext) -> dict[str, bool]:
        """Run until released or canceled; the wait loop genuinely halts."""
        while not self.release.is_set():
            if ctx.cancel_requested:
                ctx.confirm_halted("wait loop exited")
            await asyncio.sleep(0.001)
        return {"finished": True}

    @command()
    async def stubborn(self, ctx: CommandContext) -> dict[str, bool]:
        """Run until released; cannot be canceled."""
        await self.release.wait()
        return {"finished": True}


class _Client:
    """Thin wire-level test client: session + captured notifications."""

    def __init__(self, session: JsonRpcSession) -> None:
        self.session = session
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.note_received = asyncio.Event()

    async def on_note(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))
        self.note_received.set()

    async def initialize(self) -> dict[str, Any]:
        result: dict[str, Any] = await self.session.request(
            "initialize",
            {
                "protocol_version": "0.2",
                "client_info": {"name": "test", "version": "0"},
                "capabilities": {},
            },
        )
        await self.session.notify("notifications/initialized", {})
        return result

    async def statuses_for(self, command_id: str) -> list[str]:
        return [
            p["status"]
            for m, p in self.notifications
            if m == "notifications/command_status" and p["command_id"] == command_id
        ]

    async def wait_terminal(self, command_id: str, timeout: float = 2.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for m, p in self.notifications:
                if (
                    m == "notifications/command_status"
                    and p["command_id"] == command_id
                    and p["status"] in {"succeeded", "failed", "canceled"}
                ):
                    return p
            remaining = deadline - asyncio.get_running_loop().time()
            assert remaining > 0, f"no terminal status for {command_id}"
            self.note_received.clear()
            try:
                await asyncio.wait_for(self.note_received.wait(), timeout=remaining)
            except TimeoutError:
                continue


def make_rig_server() -> tuple[Rig, InstrumentServer]:
    rig = Rig()
    server = InstrumentServer(rig, clock=FakeClock())
    return rig, server


@pytest.fixture
async def connected() -> AsyncIterator[tuple[Rig, _Client]]:
    rig, server = make_rig_server()
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    session = JsonRpcSession(client_end)
    client = _Client(session)
    session._notification_handler = client.on_note  # pyright: ignore[reportPrivateUsage]
    async with session:
        await client.initialize()
        yield rig, client


async def test_initialize_result_and_gating() -> None:
    _rig, server = make_rig_server()
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with JsonRpcSession(client_end) as session:
        # ping works before initialization
        assert await session.request("ping", {}) == {}
        # anything else is rejected, not retryable
        with pytest.raises(BusyError) as excinfo:
            await session.request("instrument/describe", {})
        assert excinfo.value.retryable is False
        result = await session.request(
            "initialize",
            {
                "protocol_version": "0.2",
                "client_info": {"name": "t", "version": "0"},
                "capabilities": {},
            },
        )
        assert result["protocol_version"] == "0.4"
        # resources and grants advertise False until the server implements
        # them (SPEC §6.1): a not-yet-implementing server must say so.
        assert result["capabilities"] == {
            "telemetry": True,
            "events": True,
            "manifests": False,
            "resources": False,
            "grants": False,
        }
        await session.notify("notifications/initialized", {})
        desc = await session.request("instrument/describe", {})
        assert desc["identity"]["model"] == "TestRig-1"


async def test_duplicate_initialize_rejected(connected: tuple[Rig, _Client]) -> None:
    _rig, client = connected
    with pytest.raises(Exception, match=r"(?i)invalid request"):
        await client.initialize()


async def test_submit_runs_to_succeeded_with_result(connected: tuple[Rig, _Client]) -> None:
    _rig, client = connected
    submit = await client.session.request(
        "command/submit", {"command": "add", "params": {"a": 2.0, "b": 3.0}}
    )
    assert submit["status"] == "accepted"
    terminal = await client.wait_terminal(submit["command_id"])
    assert terminal["status"] == "succeeded"
    assert terminal["result"] == {"sum": 5.0}
    statuses = await client.statuses_for(submit["command_id"])
    assert statuses[0] == "running"
    assert statuses[-1] == "succeeded"


async def test_unknown_command_is_unsupported(connected: tuple[Rig, _Client]) -> None:
    _rig, client = connected
    with pytest.raises(UnsupportedError):
        await client.session.request("command/submit", {"command": "nope", "params": {}})


async def test_bad_params_is_validation_error(connected: tuple[Rig, _Client]) -> None:
    _rig, client = connected
    with pytest.raises(ValidationError):
        await client.session.request(
            "command/submit", {"command": "add", "params": {"a": "not a number"}}
        )


async def test_status_polling_works_after_terminal(connected: tuple[Rig, _Client]) -> None:
    _rig, client = connected
    submit = await client.session.request(
        "command/submit", {"command": "add", "params": {"a": 1.0, "b": 1.0}}
    )
    await client.wait_terminal(submit["command_id"])
    polled = await client.session.request("command/status", {"command_id": submit["command_id"]})
    assert polled["status"] == "succeeded"
    assert polled["result"] == {"sum": 2.0}


async def test_status_unknown_id_is_validation_error(connected: tuple[Rig, _Client]) -> None:
    _rig, client = connected
    with pytest.raises(ValidationError):
        await client.session.request("command/status", {"command_id": "bogus"})


async def test_cancel_interruptible_run(connected: tuple[Rig, _Client]) -> None:
    _rig, client = connected
    submit = await client.session.request(
        "command/submit", {"command": "wait_for_release", "params": {}}
    )
    await asyncio.sleep(0.01)
    reply = await client.session.request("command/cancel", {"command_id": submit["command_id"]})
    assert reply["status"] == "canceling"
    terminal = await client.wait_terminal(submit["command_id"])
    assert terminal["status"] == "canceled"
    assert terminal["cancellation"]["outcome"] == "halted"
    assert terminal["cancellation"]["requested_at"]


async def test_cancel_unknown_id_is_validation_error(connected: tuple[Rig, _Client]) -> None:
    _rig, client = connected
    with pytest.raises(ValidationError):
        await client.session.request("command/cancel", {"command_id": "bogus"})


async def test_cancel_terminal_run_is_not_cancelable(connected: tuple[Rig, _Client]) -> None:
    _rig, client = connected
    submit = await client.session.request(
        "command/submit", {"command": "add", "params": {"a": 1.0, "b": 1.0}}
    )
    await client.wait_terminal(submit["command_id"])
    with pytest.raises(NotCancelableError):
        await client.session.request("command/cancel", {"command_id": submit["command_id"]})


async def test_cancel_noninterruptible_running_is_not_cancelable(
    connected: tuple[Rig, _Client],
) -> None:
    rig, client = connected
    submit = await client.session.request("command/submit", {"command": "stubborn", "params": {}})
    await asyncio.sleep(0.01)
    with pytest.raises(NotCancelableError):
        await client.session.request("command/cancel", {"command_id": submit["command_id"]})
    rig.release.set()
    terminal = await client.wait_terminal(submit["command_id"])
    assert terminal["status"] == "succeeded"


async def test_busy_at_capacity_with_retry_hint(connected: tuple[Rig, _Client]) -> None:
    rig, client = connected
    await client.session.request("command/submit", {"command": "wait_for_release", "params": {}})
    with pytest.raises(BusyError) as excinfo:
        await client.session.request(
            "command/submit", {"command": "add", "params": {"a": 1.0, "b": 1.0}}
        )
    assert excinfo.value.retryable is True
    rig.release.set()


async def test_handler_exception_becomes_failed_status(connected: tuple[Rig, _Client]) -> None:
    class Exploder(Instrument):
        identity = IdentityInfo(
            manufacturer="m", model="x", serial_number="s", firmware_version="1"
        )

        @command()
        async def boom(self, ctx: CommandContext) -> None:
            """Explode with an internal secret."""
            raise RuntimeError("secret internal path")

    server = InstrumentServer(Exploder(), clock=FakeClock())
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    session = JsonRpcSession(client_end)
    client = _Client(session)
    session._notification_handler = client.on_note  # pyright: ignore[reportPrivateUsage]
    async with session:
        await client.initialize()
        submit = await session.request("command/submit", {"command": "boom", "params": {}})
        terminal = await client.wait_terminal(submit["command_id"])
        assert terminal["status"] == "failed"
        assert terminal["error"]["code"] == -32008
        assert "secret" not in terminal["error"]["message"]
