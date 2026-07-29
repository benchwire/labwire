"""End-to-end: a live ophyd device served through the Labwire protocol."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from labwire.bridges.ophyd import AnnotationFile, OphydInstrument
from labwire.core import (
    ConfirmationRequiredError,
    HardwareFaultError,
    InstrumentServer,
    LabwireClient,
    MemoryTransport,
    NotCancelableError,
    verify_bundle,
)
from ophyd import Component as Cpt
from ophyd import Device, Kind, Signal
from ophyd.sim import SynAxis, SynGauss

GRANT = "bridge-test-grant"

AXIS_ANNOTATIONS: dict[str, Any] = {
    "version": 1,
    "devices": {
        "ophyd.sim.SynAxis": {
            "description": "A simulated single-axis motor.",
            "intent_tags": ["motion"],
            "components": {
                "readback": {"unit": "mm", "qudt_quantity_kind": "Length"},
                "setpoint": {"unit": "mm"},
                "velocity": {"unit": "mm/s"},
                "acceleration": {"unit": "mm/s2"},
            },
        }
    },
}


class BreakingSignal(Signal):
    """A signal whose set() fails, the way a real device refuses a move."""

    def set(self, value: object, **kwargs: object) -> object:
        raise RuntimeError("motor controller says no")


class BreakingDevice(Device):
    """A device that cannot be set."""

    val = Cpt(BreakingSignal, value=0.0, kind=Kind.hinted)


async def _serve(
    device: Any, annotations: dict[str, Any], **server_kwargs: Any
) -> tuple[InstrumentServer, LabwireClient]:
    instrument = OphydInstrument(device, AnnotationFile.model_validate(annotations))
    server = InstrumentServer(instrument, confirmation_token=GRANT, **server_kwargs)
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    client = LabwireClient.attach(client_end)
    await client.__aenter__()
    return server, client


@pytest.fixture
async def axis() -> AsyncIterator[tuple[SynAxis, LabwireClient]]:
    device = SynAxis(name="ax", delay=0.05)
    server, client = await _serve(device, AXIS_ANNOTATIONS)
    yield device, client
    await client.close()
    await server.aclose()


# --- discovery -------------------------------------------------------------


async def test_the_device_is_described_in_labwire_terms(
    axis: tuple[SynAxis, LabwireClient],
) -> None:
    _device, client = axis
    descriptor = await client.describe()
    assert descriptor.identity.model == "SynAxis"
    assert descriptor.identity.serial_number == "ax"
    channels = {c.name: c for c in descriptor.channels}
    assert channels["ax"].unit == "mm"
    assert channels["ax"].qudt_quantity_kind == "Length"
    commands = {c.name: c for c in descriptor.commands}
    assert commands["move"].safety_class == "S2"
    assert commands["move"].unit_annotations == {"value": "mm"}
    assert commands["stop"].safety_class == "S0"
    assert commands["trigger"].safety_class == "S1"


async def test_an_underannotated_device_is_refused_at_construction() -> None:
    from labwire.bridges.ophyd import AnnotationError

    with pytest.raises(AnnotationError, match="unit"):
        OphydInstrument(SynAxis(name="bare"), AnnotationFile())


# --- commands --------------------------------------------------------------


async def test_setting_a_component_moves_the_device(
    axis: tuple[SynAxis, LabwireClient],
) -> None:
    device, client = axis
    handle = await client.submit("move", {"value": 4.0}, confirmation=GRANT)
    result = await handle.result(timeout=20.0)
    assert result["value"] == pytest.approx(4.0)
    reading: Any = device.read()
    assert reading["ax"]["value"] == pytest.approx(4.0)


async def test_setting_without_confirmation_is_refused(
    axis: tuple[SynAxis, LabwireClient],
) -> None:
    """Actuation is S2: an agent cannot move hardware without confirmation."""
    _device, client = axis
    with pytest.raises(ConfirmationRequiredError):
        await client.submit("move", {"value": 1.0})


async def test_read_returns_every_channel(axis: tuple[SynAxis, LabwireClient]) -> None:
    _device, client = axis
    handle = await client.submit("read", {})
    result = await handle.result(timeout=10.0)
    assert set(result) == {"ax", "ax_setpoint"}
    assert isinstance(result["ax"], float)


async def test_telemetry_streams_the_read_channels(
    axis: tuple[SynAxis, LabwireClient],
) -> None:
    _device, client = axis
    async with client.telemetry(["ax"]) as subscription:
        await client.submit("move", {"value": 2.0}, confirmation=GRANT)
        sample = await asyncio.wait_for(anext(subscription), timeout=10.0)
        assert sample.channel == "ax"
        assert isinstance(sample.value, float)


async def test_cancel_on_a_sim_axis_move_is_refused(
    axis: tuple[SynAxis, LabwireClient],
) -> None:
    """ophyd.sim's stop() is literally ``pass``: the sim move completes no
    matter what, so the honest declaration is cancel_semantics none and
    the honest answer to cancel is refusal (SPEC 8.3, F10).
    """
    device = SynAxis(name="slow", delay=2.0)
    annotations = json.loads(json.dumps(AXIS_ANNOTATIONS))
    server, client = await _serve(device, annotations)
    try:
        handle = await client.submit("move", {"value": 50.0}, confirmation=GRANT)
        await asyncio.sleep(0.2)
        with pytest.raises(NotCancelableError) as caught:
            await handle.cancel()
        assert caught.value.details == {"cancel_semantics": "none", "state": "running"}
        result = await handle.result(timeout=20.0)
        assert result["value"] == 50.0
    finally:
        await client.close()
        await server.aclose()


async def test_a_failing_set_becomes_a_hardware_fault() -> None:
    annotations = {
        "version": 1,
        "devices": {
            f"{BreakingDevice.__module__}.BreakingDevice": {"components": {"val": {"unit": "mm"}}}
        },
    }
    server, client = await _serve(BreakingDevice(name="broken"), annotations)
    try:
        handle = await client.submit("set_val", {"value": 1.0}, confirmation=GRANT)
        with pytest.raises(HardwareFaultError, match="controller says no"):
            await handle.result(timeout=10.0)
    finally:
        await client.close()
        await server.aclose()


async def test_a_blocking_device_does_not_stall_the_event_loop() -> None:
    """ophyd is synchronous: its calls must not block the server's loop."""
    device = SynAxis(name="slow", delay=1.0)
    server, client = await _serve(device, json.loads(json.dumps(AXIS_ANNOTATIONS)))
    try:
        await client.submit("move", {"value": 30.0}, confirmation=GRANT)
        await asyncio.sleep(0.1)  # the move is in flight
        # the session stays responsive while ophyd blocks in its own thread
        await asyncio.wait_for(client.ping(), timeout=2.0)
        descriptor = await asyncio.wait_for(client.describe(), timeout=2.0)
        assert descriptor.identity.model == "SynAxis"
    finally:
        await client.close()
        await server.aclose()


# --- detectors and numpy ---------------------------------------------------


async def test_trigger_acquires_from_a_detector() -> None:
    motor = SynAxis(name="m")
    detector = SynGauss(name="det", motor=motor, motor_field="m", center=0, Imax=5, sigma=1)
    annotations = {
        "version": 1,
        "devices": {
            "ophyd.sim.SynGauss": {
                "components": {
                    "val": {"unit": "{counts}"},
                    "Imax": {"unit": "{counts}"},
                    "center": {"unit": "mm"},
                    "sigma": {"unit": "mm"},
                    "noise_multiplier": {"unit": "1"},
                    "noise": {"exclude": True},  # an enum signal, not a scalar
                }
            }
        },
    }
    server, client = await _serve(detector, annotations)
    try:
        handle = await client.submit("trigger", {})  # S1: no confirmation needed
        result = await handle.result(timeout=10.0)
        assert isinstance(result["det"], float)
    finally:
        await client.close()
        await server.aclose()


async def test_numpy_values_survive_the_wire_and_a_signed_bundle(tmp_path: Path) -> None:
    """SynGauss reads produce numpy scalars; they must not corrupt a manifest."""
    motor = SynAxis(name="m")
    detector = SynGauss(name="det", motor=motor, motor_field="m", center=0, Imax=5, sigma=1)
    annotations = {
        "version": 1,
        "devices": {
            "ophyd.sim.SynGauss": {
                "components": {
                    "val": {"unit": "{counts}"},
                    "Imax": {"unit": "{counts}"},
                    "center": {"unit": "mm"},
                    "sigma": {"unit": "mm"},
                    "noise_multiplier": {"unit": "1"},
                    "noise": {"exclude": True},
                }
            }
        },
    }
    runs = tmp_path / "runs"
    server, client = await _serve(detector, annotations, manifest_dir=runs)
    try:
        handle = await client.submit("trigger", {})
        result = await handle.result(timeout=10.0)
        assert type(result["det"]) is float  # a plain JSON number, not np.float64
        bundle = runs / handle.command_id
        outcome = verify_bundle(bundle)
        assert outcome.ok, outcome.errors
        doc = json.loads((bundle / "manifest.json").read_text())
        assert "np.float64" not in json.dumps(doc)
    finally:
        await client.close()
        await server.aclose()
