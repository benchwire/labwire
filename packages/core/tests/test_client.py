"""End-to-end tests: LabwireClient against InstrumentServer over memory transport."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from labwire.core.capabilities import IdentityInfo, InstrumentDescriptor
from labwire.core.client import LabwireClient, TelemetrySample
from labwire.core.errors import CanceledError, HardwareFaultError
from labwire.core.server import (
    CommandContext,
    Instrument,
    InstrumentServer,
    channel,
    command,
)
from labwire.core.transport import MemoryTransport


class Stirrer(Instrument):
    """A stirrer with success, failure, streaming, and long-running commands."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="SimStirrer-10",
        serial_number="SIM-0007",
        firmware_version="0.1.0",
    )

    rpm = channel("rpm", unit="1/min", description="Rotor speed.")

    @command(units={"target_rpm": "1/min"}, returns_units={"reached_rpm": "1/min"})
    async def spin(self, ctx: CommandContext, target_rpm: float) -> dict[str, float]:
        """Spin up, streaming rpm, and report the reached speed."""
        for step in (0.25, 0.5, 0.75, 1.0):
            self.rpm.publish(target_rpm * step)
            await ctx.progress(step)
            await asyncio.sleep(0)
        return {"reached_rpm": target_rpm}

    @command()
    async def jam(self, ctx: CommandContext) -> None:
        """Fail with a hardware fault."""
        raise HardwareFaultError("rotor jammed")

    @command()
    async def stir_forever(self, ctx: CommandContext) -> dict[str, bool]:
        """Stir until canceled."""
        while not ctx.cancel_requested:  # noqa: ASYNC110 - polls the ctx cancel flag
            await asyncio.sleep(0.001)
        return {"stopped": True}

    @command()
    async def announce(self, ctx: CommandContext) -> None:
        """Emit a warning event."""
        ctx.emit_event("x-sim/announcement", "warning", {"note": "hello"})


@pytest.fixture(params=["memory", "websocket"])
async def client(request: pytest.FixtureRequest) -> AsyncIterator[LabwireClient]:
    server = InstrumentServer(Stirrer())
    if request.param == "memory":
        client_end, server_end = MemoryTransport.pair()
        server.attach(server_end)
        async with LabwireClient.attach(client_end) as connected:
            yield connected
    else:
        async with server.serve_websocket("127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            async with await LabwireClient.connect(f"ws://127.0.0.1:{port}") as connected:
                yield connected


async def test_handshake_exposes_server_info_and_capabilities(client: LabwireClient) -> None:
    assert client.server_info is not None
    assert client.server_info.name == "labwire-server"
    assert client.capabilities is not None
    assert client.capabilities.telemetry is True


async def test_describe_returns_typed_descriptor(client: LabwireClient) -> None:
    desc = await client.describe()
    assert isinstance(desc, InstrumentDescriptor)
    assert desc.identity.model == "SimStirrer-10"
    assert {c.name for c in desc.commands} >= {"spin", "jam", "stir_forever"}


async def test_ping(client: LabwireClient) -> None:
    await client.ping()


async def test_submit_and_await_result(client: LabwireClient) -> None:
    handle = await client.submit("spin", {"target_rpm": 300.0})
    result = await handle.result(timeout=2.0)
    assert result == {"reached_rpm": 300.0}


async def test_updates_iterator_yields_progress_then_terminal(client: LabwireClient) -> None:
    handle = await client.submit("spin", {"target_rpm": 100.0})
    statuses = [status async for status in handle.updates()]
    assert statuses[-1].status == "succeeded"
    fractions = [s.progress.fraction for s in statuses if s.progress is not None]
    assert fractions == [0.25, 0.5, 0.75, 1.0]


async def test_failed_command_raises_typed_error(client: LabwireClient) -> None:
    handle = await client.submit("jam", {})
    with pytest.raises(HardwareFaultError, match="rotor jammed"):
        await handle.result(timeout=2.0)


async def test_cancel_via_handle(client: LabwireClient) -> None:
    handle = await client.submit("stir_forever", {})
    await asyncio.sleep(0.01)
    await handle.cancel()
    with pytest.raises(CanceledError):
        await handle.result(timeout=2.0)


async def test_telemetry_subscription_yields_typed_samples(client: LabwireClient) -> None:
    async with client.telemetry(["rpm"]) as subscription:
        await client.submit("spin", {"target_rpm": 200.0})
        samples: list[TelemetrySample] = []
        async for sample in subscription:
            samples.append(sample)
            if len(samples) == 4:
                break
    assert [s.value for s in samples] == [50.0, 100.0, 150.0, 200.0]
    assert [s.seq for s in samples] == [1, 2, 3, 4]
    assert all(s.channel == "rpm" for s in samples)


async def test_events_iterator(client: LabwireClient) -> None:
    events = client.events()
    await client.submit("announce", {})
    event = await asyncio.wait_for(anext(events), timeout=2.0)
    assert event.name == "x-sim/announcement"
    assert event.severity == "warning"
    assert event.data == {"note": "hello"}


async def test_client_close_is_clean() -> None:
    server = InstrumentServer(Stirrer())
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    connected = LabwireClient.attach(client_end)
    async with connected:
        await connected.ping()
    # after close, further requests fail fast
    with pytest.raises(Exception, match=r"(?i)closed"):
        await connected.ping()
