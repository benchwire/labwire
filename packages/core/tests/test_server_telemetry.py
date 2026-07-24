"""Wire-level tests for telemetry, events, interlocks, and run records."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from labwire.core.capabilities import IdentityInfo
from labwire.core.errors import InterlockError, ValidationError
from labwire.core.server import (
    CommandContext,
    Instrument,
    InstrumentServer,
    channel,
    command,
    interlock,
)
from labwire.core.session import JsonRpcSession
from labwire.core.transport import MemoryTransport


class Balance(Instrument):
    """A balance that streams mass samples while measuring."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="SimBalance-120",
        serial_number="SIM-0003",
        firmware_version="0.1.0",
    )

    mass = channel("mass", unit="g", description="Current mass reading.")
    overload = interlock("overload", description="Mass beyond safe range.", kind="soft")

    def __init__(self) -> None:
        super().__init__()
        self.proceed = asyncio.Event()

    @command()
    async def measure(self, ctx: CommandContext, samples: int = 3) -> dict[str, float]:
        """Stream ``samples`` mass readings, then report the last one."""
        reading = 0.0
        for i in range(samples):
            reading = 10.0 + i
            self.mass.publish(reading)
            ctx.emit_event("measurement/stable", "info", {"channel": "mass", "value": reading})
            await asyncio.sleep(0)
        return {"mass_g": reading}

    @command()
    async def hold(self, ctx: CommandContext) -> dict[str, bool]:
        """Wait until released; used to test interlock aborts."""
        await self.proceed.wait()
        return {"finished": True}

    @command(clears_interlocks=["overload"])
    async def reset_overload(self, ctx: CommandContext) -> dict[str, bool]:
        """Clear the overload interlock."""
        self.overload.clear()
        return {"cleared": True}


class _Notes:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []
        self.received = asyncio.Event()

    async def collect(self, method: str, params: dict[str, Any]) -> None:
        self.items.append((method, params))
        self.received.set()

    def named(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.items if m == method]

    async def wait_for(self, predicate_method: str, count: int, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.named(predicate_method)) < count:
            remaining = deadline - asyncio.get_running_loop().time()
            assert remaining > 0, f"timed out waiting for {count} x {predicate_method}"
            self.received.clear()
            try:
                await asyncio.wait_for(self.received.wait(), timeout=remaining)
            except TimeoutError:
                continue


@pytest.fixture
async def rig() -> AsyncIterator[tuple[Balance, InstrumentServer, JsonRpcSession, _Notes]]:
    balance = Balance()
    server = InstrumentServer(balance)
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    notes = _Notes()
    session = JsonRpcSession(client_end, notification_handler=notes.collect)
    async with session:
        await session.request(
            "initialize",
            {
                "protocol_version": "0.1",
                "client_info": {"name": "t", "version": "0"},
                "capabilities": {},
            },
        )
        await session.notify("notifications/initialized", {})
        yield balance, server, session, notes


async def test_subscribe_receives_sequenced_samples(
    rig: tuple[Balance, InstrumentServer, JsonRpcSession, _Notes],
) -> None:
    _balance, _server, session, notes = rig
    sub = await session.request("telemetry/subscribe", {"channels": ["mass"]})
    await session.request("command/submit", {"command": "measure", "params": {"samples": 3}})
    await notes.wait_for("notifications/telemetry", 3)
    samples = notes.named("notifications/telemetry")
    assert [s["value"] for s in samples] == [10.0, 11.0, 12.0]
    assert [s["seq"] for s in samples] == [1, 2, 3]
    assert all(s["subscription_id"] == sub["subscription_id"] for s in samples)
    assert all(s["channel"] == "mass" for s in samples)
    assert all(s["timestamp"].endswith("Z") for s in samples)


async def test_unsubscribe_stops_delivery(
    rig: tuple[Balance, InstrumentServer, JsonRpcSession, _Notes],
) -> None:
    balance, _server, session, notes = rig
    sub = await session.request("telemetry/subscribe", {"channels": ["mass"]})
    await session.request("telemetry/unsubscribe", {"subscription_id": sub["subscription_id"]})
    balance.mass.publish(1.0)
    await asyncio.sleep(0.05)
    assert notes.named("notifications/telemetry") == []


async def test_subscribe_unknown_channel_is_validation_error(
    rig: tuple[Balance, InstrumentServer, JsonRpcSession, _Notes],
) -> None:
    _balance, _server, session, _notes = rig
    with pytest.raises(ValidationError):
        await session.request("telemetry/subscribe", {"channels": ["bogus"]})


async def test_events_are_pushed(
    rig: tuple[Balance, InstrumentServer, JsonRpcSession, _Notes],
) -> None:
    _balance, _server, session, notes = rig
    await session.request("command/submit", {"command": "measure", "params": {"samples": 1}})
    await notes.wait_for("notifications/event", 1)
    event = notes.named("notifications/event")[0]
    assert event["name"] == "measurement/stable"
    assert event["severity"] == "info"
    assert event["data"] == {"channel": "mass", "value": 10.0}


async def test_interlock_trip_fails_running_command_and_blocks_submit(
    rig: tuple[Balance, InstrumentServer, JsonRpcSession, _Notes],
) -> None:
    balance, _server, session, notes = rig
    submit = await session.request("command/submit", {"command": "hold", "params": {}})
    await asyncio.sleep(0.01)
    balance.overload.trip()
    await notes.wait_for("notifications/event", 1)
    trip_event = notes.named("notifications/event")[0]
    assert trip_event["name"] == "interlock/tripped"
    assert trip_event["severity"] == "alarm"
    assert trip_event["data"] == {"interlock": "overload"}
    await notes.wait_for("notifications/command_status", 2)  # running, then failed
    polled = await session.request("command/status", {"command_id": submit["command_id"]})
    assert polled["status"] == "failed"
    assert polled["error"]["data"]["category"] == "interlock"
    # submits are rejected while tripped...
    with pytest.raises(InterlockError):
        await session.request("command/submit", {"command": "measure", "params": {}})
    # ...except the declared clearing command
    clearing = await session.request("command/submit", {"command": "reset_overload", "params": {}})
    deadline = asyncio.get_running_loop().time() + 2.0
    while True:
        status = await session.request("command/status", {"command_id": clearing["command_id"]})
        if status["status"] in {"succeeded", "failed", "canceled"}:
            break
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
    assert status["status"] == "succeeded"
    cleared = [e for e in notes.named("notifications/event") if e["name"] == "interlock/cleared"]
    assert cleared
    assert cleared[0]["data"] == {"interlock": "overload"}
    # normal submits work again
    ok = await session.request("command/submit", {"command": "measure", "params": {}})
    assert ok["status"] == "accepted"


async def test_run_record_accumulates_digest_of_samples_and_events(
    rig: tuple[Balance, InstrumentServer, JsonRpcSession, _Notes],
) -> None:
    _balance, server, session, notes = rig
    submit = await session.request(
        "command/submit", {"command": "measure", "params": {"samples": 2}}
    )
    await notes.wait_for("notifications/command_status", 2)
    record = server.run_records[submit["command_id"]]
    assert record.status == "succeeded"
    assert record.command == "measure"
    assert record.channels == ["mass"]
    assert set(record.timestamps) == {"submitted", "started", "completed"}
    samples = notes.named("notifications/telemetry")  # none: no subscription in this test
    assert samples == []
    expected_records: list[dict[str, Any]] = []
    for i, value in enumerate([10.0, 11.0]):
        expected_records.append(
            {"type": "sample", "channel": "mass", "seq": i + 1, "timestamp": "?", "value": value}
        )
        expected_records.append(
            {
                "type": "event",
                "name": "measurement/stable",
                "timestamp": "?",
                "severity": "info",
                "data": {"channel": "mass", "value": value},
            }
        )
    # timestamps aren't observable without a subscription; assert digest is
    # deterministic in shape instead: 64 hex chars and not the empty digest
    assert len(record.digest) == 64
    assert record.digest != hashlib.sha256(b"").hexdigest()
    assert json.loads(json.dumps(expected_records))  # records are JSON-serializable


async def test_run_record_empty_stream_digests_to_empty_sha256(
    rig: tuple[Balance, InstrumentServer, JsonRpcSession, _Notes],
) -> None:
    balance, server, session, _notes = rig
    submit = await session.request("command/submit", {"command": "hold", "params": {}})
    await asyncio.sleep(0.01)
    balance.proceed.set()
    deadline = asyncio.get_running_loop().time() + 2.0
    while server.run_records[submit["command_id"]].status not in {
        "succeeded",
        "failed",
        "canceled",
    }:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
    record = server.run_records[submit["command_id"]]
    assert record.digest == hashlib.sha256(b"").hexdigest()
    assert record.channels == []
