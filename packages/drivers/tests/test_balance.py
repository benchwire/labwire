"""End-to-end: Balance driver against SimBalance over real TCP."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from labwire.core import (
    DeviceTimeoutError,
    InstrumentServer,
    InterlockError,
    LabwireClient,
    MemoryTransport,
)
from labwire.drivers import Balance
from labwire.sim import SimBalance


@pytest.fixture
async def rig() -> AsyncIterator[tuple[SimBalance, LabwireClient]]:
    sim = SimBalance(seed=3)
    await sim.start()
    server = InstrumentServer(Balance("127.0.0.1", sim.port))
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        yield sim, client
    await server.aclose()
    await sim.stop()


async def _run(client: LabwireClient, name: str, params: dict[str, object]) -> dict[str, float]:
    handle = await client.submit(name, dict(params))
    result: dict[str, float] = await handle.result(timeout=10.0)
    return result


async def test_descriptor_declares_the_balance(rig: tuple[SimBalance, LabwireClient]) -> None:
    _sim, client = rig
    desc = await client.describe()
    assert desc.identity.model == "SimBalance-120"
    names = {c.name for c in desc.commands}
    assert {"measure", "tare", "x-sim/load", "x-sim/inject_fault"} <= names
    assert [c.name for c in desc.channels] == ["mass"]
    assert desc.channels[0].unit == "g"
    assert [i.name for i in desc.interlocks] == ["overload"]


async def test_measure_settles_to_loaded_mass_and_emits_stable_event(
    rig: tuple[SimBalance, LabwireClient],
) -> None:
    _sim, client = rig
    async with client.events() as events:
        await _run(client, "x-sim/load", {"mass_g": 12.3456})
        result = await _run(client, "measure", {})
        assert result["mass_g"] == pytest.approx(12.3456, abs=0.01)
        while True:
            event = await asyncio.wait_for(anext(events), timeout=5.0)
            if event.name == "measurement/stable":
                assert event.data["value"] == pytest.approx(12.3456, abs=0.01)
                break


async def test_mass_channel_streams_settling_readings(
    rig: tuple[SimBalance, LabwireClient],
) -> None:
    _sim, client = rig
    async with client.telemetry(["mass"]) as sub:
        await _run(client, "x-sim/load", {"mass_g": 50.0})
        deadline = asyncio.get_running_loop().time() + 5.0
        latest = 0.0
        while asyncio.get_running_loop().time() < deadline:
            sample = await asyncio.wait_for(anext(sub), timeout=5.0)
            latest = float(sample.value)
            if latest == pytest.approx(50.0, abs=0.01):
                break
        assert latest == pytest.approx(50.0, abs=0.01)


async def test_tare_zeroes_the_reading(rig: tuple[SimBalance, LabwireClient]) -> None:
    _sim, client = rig
    await _run(client, "x-sim/load", {"mass_g": 5.0})
    await _run(client, "tare", {})
    assert (await _run(client, "measure", {}))["mass_g"] == pytest.approx(0.0, abs=0.01)
    await _run(client, "x-sim/load", {"mass_g": 12.0})  # 7 g net on top of the tare
    assert (await _run(client, "measure", {}))["mass_g"] == pytest.approx(7.0, abs=0.01)


async def test_vibration_prevents_stability_within_timeout(
    rig: tuple[SimBalance, LabwireClient],
) -> None:
    _sim, client = rig
    await _run(client, "x-sim/inject_fault", {"kind": "vibration"})
    handle = await client.submit("measure", {"settle_timeout_s": 0.5})
    with pytest.raises(DeviceTimeoutError):
        await handle.result(timeout=10.0)


async def test_overload_trips_and_clears_with_the_load(
    rig: tuple[SimBalance, LabwireClient],
) -> None:
    _sim, client = rig
    await _run(client, "x-sim/load", {"mass_g": 500.0})  # capacity is 120 g
    deadline = asyncio.get_running_loop().time() + 5.0
    while not (await client.describe()).interlocks[0].tripped:
        assert asyncio.get_running_loop().time() < deadline, "overload never tripped"
        await asyncio.sleep(0.05)
    with pytest.raises(InterlockError):
        await client.submit("measure", {})
    await _run(client, "x-sim/load", {"mass_g": 10.0})  # removing the mass clears it
    deadline = asyncio.get_running_loop().time() + 5.0
    while (await client.describe()).interlocks[0].tripped:
        assert asyncio.get_running_loop().time() < deadline, "overload never cleared"
        await asyncio.sleep(0.05)
    assert (await _run(client, "measure", {}))["mass_g"] == pytest.approx(10.0, abs=0.01)
