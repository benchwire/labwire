"""End-to-end: PowerSupply driver against SimPowerSupply over SCPI/TCP."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from labwire.core import (
    InstrumentServer,
    InterlockError,
    LabwireClient,
    MemoryTransport,
)
from labwire.drivers import PowerSupply
from labwire.sim import SimPowerSupply


@pytest.fixture
async def rig() -> AsyncIterator[tuple[SimPowerSupply, LabwireClient]]:
    sim = SimPowerSupply(seed=7)
    await sim.start()
    server = InstrumentServer(PowerSupply("127.0.0.1", sim.port))
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


async def test_descriptor_declares_the_psu(rig: tuple[SimPowerSupply, LabwireClient]) -> None:
    _sim, client = rig
    desc = await client.describe()
    assert desc.identity.model == "SimPSU-3005"
    names = {c.name for c in desc.commands}
    assert {
        "set_voltage",
        "set_current_limit",
        "output",
        "measure",
        "clear_protection",
        "x-sim/set_load",
        "x-sim/inject_fault",
    } <= names
    assert {c.name for c in desc.channels} == {"voltage", "current"}
    assert [i.name for i in desc.interlocks] == ["over_current"]


async def test_voltage_settles_to_setpoint_under_load(
    rig: tuple[SimPowerSupply, LabwireClient],
) -> None:
    _sim, client = rig
    await _run(client, "set_current_limit", {"amps": 2.0})
    await _run(client, "output", {"on": True})
    result = await _run(client, "set_voltage", {"volts": 12.0})
    assert result["volts"] == pytest.approx(12.0, rel=0.02)
    measured = await _run(client, "measure", {})
    # default simulated load is 100 ohms: I = V/R
    assert measured["amps"] == pytest.approx(0.12, rel=0.05)


async def test_current_limit_forces_constant_current_mode(
    rig: tuple[SimPowerSupply, LabwireClient],
) -> None:
    _sim, client = rig
    await _run(client, "x-sim/set_load", {"ohms": 10.0})
    await _run(client, "set_current_limit", {"amps": 0.5})
    await _run(client, "output", {"on": True})
    await _run(client, "set_voltage", {"volts": 12.0})
    measured = await _run(client, "measure", {})
    # 12 V into 10 ohms would be 1.2 A: limited to 0.5 A -> 5 V across the load
    assert measured["amps"] == pytest.approx(0.5, rel=0.05)
    assert measured["volts"] == pytest.approx(5.0, rel=0.05)


async def test_telemetry_streams_voltage_while_output_on(
    rig: tuple[SimPowerSupply, LabwireClient],
) -> None:
    _sim, client = rig
    await _run(client, "output", {"on": True})
    await _run(client, "set_voltage", {"volts": 5.0})
    async with client.telemetry(["voltage"]) as sub:
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            sample = await asyncio.wait_for(anext(sub), timeout=5.0)
            if float(sample.value) == pytest.approx(5.0, rel=0.05):
                return
        pytest.fail("voltage channel never reached the setpoint")


async def test_ocp_trips_interlock_and_disables_output(
    rig: tuple[SimPowerSupply, LabwireClient],
) -> None:
    sim, client = rig
    await _run(client, "output", {"on": True})
    await _run(client, "set_voltage", {"volts": 12.0})
    await _run(client, "x-sim/inject_fault", {"kind": "ocp"})
    deadline = asyncio.get_running_loop().time() + 5.0
    while not (await client.describe()).interlocks[0].tripped:
        assert asyncio.get_running_loop().time() < deadline, "OCP never tripped"
        await asyncio.sleep(0.05)
    assert sim.output is False  # protection cut the output at the device
    with pytest.raises(InterlockError):
        await client.submit("set_voltage", {"volts": 5.0})
    await _run(client, "clear_protection", {})
    assert (await client.describe()).interlocks[0].tripped is False
    result = await _run(client, "output", {"on": True})
    assert result["on"] == 1.0
