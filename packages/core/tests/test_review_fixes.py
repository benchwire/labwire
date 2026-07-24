"""Regression tests for the M2 adversarial-review findings."""

import asyncio
import math
from collections.abc import AsyncIterator
from typing import Any

import pytest
from labwire.core.capabilities import IdentityInfo
from labwire.core.client import LabwireClient
from labwire.core.errors import (
    InterlockError,
    InvalidParamsError,
    MethodNotFoundError,
    ValidationError,
)
from labwire.core.jcs import jcs_dumps as _jcs_dumps
from labwire.core.server import (
    CommandContext,
    Instrument,
    InstrumentServer,
    channel,
    command,
    interlock,
)
from labwire.core.session import JsonRpcSession, SessionClosed
from labwire.core.transport import MemoryTransport


class Doser(Instrument):
    """Instrument exercising the review-fix behaviors."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="Doser-1",
        serial_number="SIM-0009",
        firmware_version="0.1.0",
    )

    flow = channel("flow", unit="uL/min", description="Flow rate.")
    lock_a = interlock("lock_a", description="First lock.", kind="soft")
    lock_b = interlock("lock_b", description="Second lock.", kind="soft")

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.started_background = False

    async def on_start(self, server: InstrumentServer) -> None:
        """Record that the lifecycle hook ran."""
        self.started_background = True

    @command()
    async def dose(self, ctx: CommandContext, volume_ul: float = 1.0) -> dict[str, float]:
        """Dose a volume."""
        return {"dosed_ul": volume_ul}

    @command()
    async def hold(self, ctx: CommandContext) -> dict[str, bool]:
        """Wait for release."""
        await self.release.wait()
        return {"finished": True}

    @command(clears_interlocks=["lock_a"])
    async def clear_a(self, ctx: CommandContext) -> dict[str, bool]:
        """Clear lock_a."""
        self.lock_a.clear()
        return {"cleared": True}

    @command(name="x-sim/inject_fault")
    async def inject_fault(self, ctx: CommandContext, kind: str) -> dict[str, str]:
        """Inject a simulated fault (vendor-extension command)."""
        return {"injected": kind}


@pytest.fixture
async def wired() -> AsyncIterator[tuple[Doser, InstrumentServer, LabwireClient]]:
    dosed = Doser()
    server = InstrumentServer(dosed)
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        yield dosed, server, client
    await server.aclose()


# --- agent-safety: strict params -------------------------------------------


async def test_misspelled_param_is_rejected_not_defaulted(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    _dosed, _server, client = wired
    with pytest.raises(ValidationError):
        await client.submit("dose", {"volume_uL": 500.0})  # typo'd key


async def test_params_schema_forbids_additional_properties(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    _dosed, _server, client = wired
    desc = await client.describe()
    schema = next(c for c in desc.commands if c.name == "dose").params_schema
    assert schema.get("additionalProperties") is False
    assert "title" not in schema


# --- error taxonomy --------------------------------------------------------


async def test_unknown_method_is_method_not_found(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    _dosed, _server, client = wired
    with pytest.raises(MethodNotFoundError):
        await client._request("no/such_method", {})  # pyright: ignore[reportPrivateUsage]


async def test_missing_command_id_is_invalid_params(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    _dosed, _server, client = wired
    with pytest.raises(InvalidParamsError):
        await client._request("command/status", {})  # pyright: ignore[reportPrivateUsage]


async def test_invalid_params_precede_interlock_and_busy(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    dosed, _server, client = wired
    await client.submit("hold", {})  # occupy the only slot
    with pytest.raises(ValidationError):  # not BusyError: validation precedes capacity
        await client.submit("dose", {"volume_ul": "not a number"})
    dosed.release.set()


async def test_validation_error_details_name_the_field(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    _dosed, _server, client = wired
    with pytest.raises(ValidationError) as excinfo:
        await client.submit("dose", {"volume_ul": "oops"})
    details = excinfo.value.details
    assert details is not None
    assert "volume_ul" in str(details["errors"])


# --- interlocks ------------------------------------------------------------


async def test_clearing_command_accepted_with_multiple_tripped_interlocks(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    dosed, _server, client = wired
    dosed.lock_a.trip()
    dosed.lock_b.trip()
    handle = await client.submit("clear_a", {})  # must be accepted despite lock_b
    assert (await handle.result(timeout=2.0)) == {"cleared": True}
    with pytest.raises(InterlockError):  # lock_b still tripped, normal command blocked
        await client.submit("dose", {})
    dosed.lock_b.clear()


async def test_interlock_trip_on_accepted_run_reaches_failed_terminal(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    dosed, server, client = wired
    handle = await client.submit("hold", {})
    # trip synchronously, before the run task has had any chance to start
    dosed.lock_a.trip()
    status = await handle.status()
    assert status.status == "failed"
    assert status.error is not None
    assert status.error.data is not None
    assert status.error.data.category == "interlock"
    record = server.run_records[handle.command_id]
    assert record.status == "failed"
    assert "started" in record.timestamps
    dosed.lock_a.clear()
    # the slot is free again: a new submit is accepted
    follow_up = await client.submit("dose", {})
    assert (await follow_up.result(timeout=2.0)) == {"dosed_ul": 1.0}


# --- lifecycle -------------------------------------------------------------


async def test_on_start_hook_runs(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    dosed, _server, _client = wired
    assert dosed.started_background is True


async def test_vendor_extension_command_name(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    _dosed, _server, client = wired
    desc = await client.describe()
    assert any(c.name == "x-sim/inject_fault" for c in desc.commands)
    handle = await client.submit("x-sim/inject_fault", {"kind": "clog"})
    assert (await handle.result(timeout=2.0)) == {"injected": "clog"}


# --- client robustness -----------------------------------------------------


async def test_client_answers_ping() -> None:
    server = InstrumentServer(Doser())
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        session = next(iter(server._sessions))  # pyright: ignore[reportPrivateUsage]
        assert await session.session.request("ping", {}) == {}
        del client


async def test_session_close_fails_pending_handle() -> None:
    dosed = Doser()
    server = InstrumentServer(dosed)
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    client = LabwireClient.attach(client_end)
    async with client:
        handle = await client.submit("hold", {})
    # session is closed with the run still in flight: result() must not hang
    with pytest.raises(SessionClosed):
        await handle.result(timeout=2.0)
    dosed.release.set()
    await server.aclose()


async def test_telemetry_iterator_ends_after_unsubscribe(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    dosed, _server, client = wired
    async with client.telemetry(["flow"]) as subscription:
        dosed.flow.publish(1.0)
        sample = await asyncio.wait_for(anext(subscription), timeout=2.0)
        assert sample.value == 1.0
    # after context exit the iterator terminates instead of hanging
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(subscription), timeout=2.0)


async def test_malformed_frame_does_not_kill_session() -> None:
    ours, theirs = MemoryTransport.pair()

    async def handler(method: str, params: dict[str, Any]) -> Any:
        return "alive"

    async with JsonRpcSession(ours, request_handler=handler):
        # string ids violate SPEC §3.1: answered -32600 with id null
        await theirs.send({"jsonrpc": "2.0", "id": "strings-are-invalid", "method": 42})
        await theirs.send({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}})
        replies = [await theirs.receive(), await theirs.receive()]
        by_id = {r.get("id"): r for r in replies}
        assert by_id[None]["error"]["code"] == -32600
        assert by_id[7]["result"] == "alive"


# --- telemetry hygiene -----------------------------------------------------


async def test_nonfinite_sample_suppressed_and_reported(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    dosed, _server, client = wired
    events = client.events()
    async with client.telemetry(["flow"]) as subscription:
        dosed.flow.publish(math.nan)
        dosed.flow.publish(2.5)
        sample = await asyncio.wait_for(anext(subscription), timeout=2.0)
        assert sample.value == 2.5
        assert sample.seq == 1  # NaN was never produced: seq not consumed
    event = await asyncio.wait_for(anext(events), timeout=2.0)
    assert event.name == "error/occurred"
    assert event.data["channel"] == "flow"


async def test_max_rate_hz_drops_intermediate_samples(
    wired: tuple[Doser, InstrumentServer, LabwireClient],
) -> None:
    dosed, _server, client = wired
    async with client.telemetry(["flow"], max_rate_hz=1.0) as subscription:
        for i in range(5):
            dosed.flow.publish(float(i))  # burst within one second
        first = await asyncio.wait_for(anext(subscription), timeout=2.0)
        assert first.value == 0.0
        # the burst is throttled: nothing else arrives promptly
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(subscription), timeout=0.1)


# --- JCS canonicalization (RFC 8785 number/string forms) --------------------


def test_jcs_number_formatting_matches_rfc8785() -> None:
    cases = [
        ({"v": 1.0}, '{"v":1}'),
        ({"v": 25.5}, '{"v":25.5}'),
        ({"v": 1e16}, '{"v":10000000000000000}'),
        ({"v": 1e21}, '{"v":1e+21}'),
        ({"v": 1e-7}, '{"v":1e-7}'),
        ({"v": 0.000001}, '{"v":0.000001}'),
        ({"v": 0}, '{"v":0}'),
        ({"v": -0.0}, '{"v":0}'),
        ({"v": True}, '{"v":true}'),
        ({"v": None}, '{"v":null}'),
        ({"v": "25°C"}, '{"v":"25°C"}'),
        ({"b": 1, "a": 2}, '{"a":2,"b":1}'),
        ({"v": [1.5, "x"]}, '{"v":[1.5,"x"]}'),
    ]
    for value, expected in cases:
        assert _jcs_dumps(value) == expected, value
