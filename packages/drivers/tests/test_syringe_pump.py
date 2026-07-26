"""End-to-end: SyringePump driver against SimSyringePump over real TCP."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from labwire.core import (
    CanceledError,
    InstrumentServer,
    InterlockError,
    LabwireClient,
    MemoryTransport,
)
from labwire.drivers import SyringePump
from labwire.sim import SimSyringePump

GRANT = "test-standing-grant"


@pytest.fixture
async def rig() -> AsyncIterator[tuple[SimSyringePump, InstrumentServer, LabwireClient]]:
    sim = SimSyringePump(seed=1)
    await sim.start()
    pump = SyringePump("127.0.0.1", sim.port)
    server = InstrumentServer(pump, confirmation_token=GRANT)
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        yield sim, server, client
    await server.aclose()
    await sim.stop()


async def test_descriptor_declares_the_pump(
    rig: tuple[SimSyringePump, InstrumentServer, LabwireClient],
) -> None:
    _sim, _server, client = rig
    desc = await client.describe()
    assert desc.identity.model == "SimPump-200"
    assert desc.identity.manufacturer == "Labwire Project"
    names = {c.name for c in desc.commands}
    assert {"dispense", "clear_occlusion", "x-sim/inject_fault"} <= names
    assert {c.name for c in desc.channels} == {"flow_rate", "dispensed"}
    assert [i.name for i in desc.interlocks] == ["occlusion"]
    dispense = next(c for c in desc.commands if c.name == "dispense")
    assert dispense.unit_annotations == {"volume_ul": "uL", "rate_ul_min": "uL/min"}


async def test_dispense_completes_with_requested_volume(
    rig: tuple[SimSyringePump, InstrumentServer, LabwireClient],
) -> None:
    _sim, _server, client = rig
    async with client.telemetry(["flow_rate", "dispensed"]) as sub:
        handle = await client.submit(
            "dispense", {"volume_ul": 200.0, "rate_ul_min": 60000.0}, confirmation=GRANT
        )
        result = await handle.result(timeout=10.0)
        assert result["dispensed_ul"] == pytest.approx(200.0, rel=0.05)
        flowing: list[float] = []
        async for sample in sub:
            if sample.channel == "flow_rate" and float(sample.value) > 0:
                flowing.append(float(sample.value))
                break
        assert flowing  # rate was streamed while dispensing


async def test_cancel_mid_dispense_stops_the_pump(
    rig: tuple[SimSyringePump, InstrumentServer, LabwireClient],
) -> None:
    sim, _server, client = rig
    handle = await client.submit(
        "dispense", {"volume_ul": 5000.0, "rate_ul_min": 6000.0}, confirmation=GRANT
    )
    await asyncio.sleep(0.3)
    await handle.cancel()
    with pytest.raises(CanceledError):
        await handle.result(timeout=5.0)
    assert sim.dispensed_ul < 5000.0
    assert sim.state == "IDLE"  # motor actually stopped


async def test_occlusion_faults_the_run_and_blocks_submits_until_cleared(
    rig: tuple[SimSyringePump, InstrumentServer, LabwireClient],
) -> None:
    _sim, _server, client = rig
    inject = await client.submit("x-sim/inject_fault", {"kind": "occlusion"})
    await inject.result(timeout=5.0)
    handle = await client.submit(
        "dispense", {"volume_ul": 1000.0, "rate_ul_min": 6000.0}, confirmation=GRANT
    )
    with pytest.raises(InterlockError):
        await handle.result(timeout=10.0)
    desc = await client.describe()
    assert desc.interlocks[0].tripped is True
    with pytest.raises(InterlockError):  # ordinary submits blocked while tripped
        await client.submit(
            "dispense", {"volume_ul": 10.0, "rate_ul_min": 6000.0}, confirmation=GRANT
        )
    clearing = await client.submit("clear_occlusion", {})
    assert (await clearing.result(timeout=5.0))["cleared"] is True
    retry = await client.submit(
        "dispense", {"volume_ul": 50.0, "rate_ul_min": 60000.0}, confirmation=GRANT
    )
    result = await retry.result(timeout=10.0)
    assert result["dispensed_ul"] == pytest.approx(50.0, rel=0.05)


async def test_fault_injected_after_cancel_does_not_stall_idle_pump(
    rig: tuple[SimSyringePump, InstrumentServer, LabwireClient],
) -> None:
    sim, _server, client = rig
    handle = await client.submit(
        "dispense", {"volume_ul": 5000.0, "rate_ul_min": 6000.0}, confirmation=GRANT
    )
    await asyncio.sleep(0.1)
    await handle.cancel()
    with pytest.raises(CanceledError):
        await handle.result(timeout=5.0)
    inject = await client.submit("x-sim/inject_fault", {"kind": "occlusion"})
    await inject.result(timeout=5.0)
    await asyncio.sleep(0.1)  # the canceled run's motor task must not wake and stall
    assert sim.state == "IDLE"


async def test_dispensed_volume_has_realistic_error(
    rig: tuple[SimSyringePump, InstrumentServer, LabwireClient],
) -> None:
    _sim, _server, client = rig
    handle = await client.submit(
        "dispense", {"volume_ul": 300.0, "rate_ul_min": 90000.0}, confirmation=GRANT
    )
    result = await handle.result(timeout=10.0)
    # realistic, seeded: close to target but not bit-exact
    assert result["dispensed_ul"] != 300.0
    assert result["dispensed_ul"] == pytest.approx(300.0, rel=0.05)
