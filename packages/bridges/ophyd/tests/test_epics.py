"""The bridge against a real EPICS Channel Access layer.

Everything else in this package talks to ``ophyd.sim`` devices, which report
no units at all. This module runs an actual soft IOC (caproto) with `.EGU`
set on its PVs, connects ophyd's EPICS signals to it over Channel Access, and
proves the thing simulation cannot: that units and control limits are
**adopted from the device** rather than supplied by hand.

The IOC is pure Python: no EPICS base install, and is bound to localhost on
an ephemeral port so it never touches a real control network.
"""

import os
import random
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

# Channel Access configuration must be in the environment before caproto's
# server or client reads it: localhost only, on a port nothing else uses.
_CA_PORT = random.randint(15064, 25064)
os.environ.setdefault("EPICS_CA_SERVER_PORT", str(_CA_PORT))
os.environ.setdefault("EPICS_CA_REPEATER_PORT", str(_CA_PORT + 1))
os.environ.setdefault("EPICS_CA_ADDR_LIST", "127.0.0.1")
os.environ.setdefault("EPICS_CA_AUTO_ADDR_LIST", "NO")

import ophyd  # noqa: E402
from caproto.server import PVGroup, pvproperty, run  # noqa: E402
from labwire.bridges.ophyd import AnnotationFile, OphydInstrument, introspect  # noqa: E402
from labwire.core import InstrumentServer, LabwireClient, MemoryTransport  # noqa: E402
from ophyd import Component as Cpt  # noqa: E402
from ophyd import Device, EpicsSignal, EpicsSignalRO, Kind  # noqa: E402

PREFIX = "LWBRIDGE:"
GRANT = "epics-test-grant"


class SoftIOC(PVGroup):
    """A minimal soft IOC: a movable axis and a read-only temperature.

    Both PVs carry engineering units and control limits, exactly as a real
    IOC would, which is what the bridge is here to read.
    """

    stage_x = pvproperty(
        value=1.5,
        units="mm",  # the EGU string a real IOC publishes
        precision=3,
        lower_ctrl_limit=-25.0,
        upper_ctrl_limit=25.0,
        doc="Simulated axis position",
    )
    temperature = pvproperty(
        value=21.5,
        units="degC",  # deliberately not valid UCUM: it must be translated
        precision=2,
        read_only=True,
        doc="Simulated sample temperature",
    )


class EpicsRig(Device):
    """An ophyd device wired to the soft IOC over Channel Access."""

    stage_x = Cpt(EpicsSignal, "stage_x", kind=Kind.hinted)
    temperature = Cpt(EpicsSignalRO, "temperature", kind=Kind.hinted)


@pytest.fixture(scope="module")
def ioc() -> Iterator[None]:
    """Run the soft IOC for the lifetime of this module."""
    previous = ophyd.get_cl()
    # caproto's control layer is pure Python; pyepics would need EPICS base.
    ophyd.set_cl("caproto")
    thread = threading.Thread(
        target=lambda: run(SoftIOC(prefix=PREFIX).pvdb, log_pv_names=False),
        daemon=True,
        name="labwire-test-ioc",
    )
    thread.start()
    time.sleep(1.5)  # let the IOC bind before the first search
    try:
        yield
    finally:
        ophyd.set_cl(previous.name if hasattr(previous, "name") else "caproto")


@pytest.fixture
def device(ioc: None) -> Iterator[EpicsRig]:
    """A connected ophyd device, or a hard failure explaining why not."""
    rig = EpicsRig(PREFIX, name="epics_rig")
    try:
        rig.wait_for_connection(timeout=20.0)
    except Exception as exc:
        pytest.fail(f"could not connect to the soft IOC over Channel Access: {exc}")
    yield rig
    rig.destroy()


def test_units_are_adopted_from_real_channel_access_metadata(device: EpicsRig) -> None:
    """The claim simulation cannot support: units come from the device."""
    draft = introspect(device)
    stage = draft.component("epics_rig_stage_x")
    assert stage.egu == "mm"  # the raw EGU string, straight off the wire
    assert stage.unit == "mm"
    assert stage.unit_source == "describe"


def test_a_non_ucum_egu_string_is_translated_not_passed_through(device: EpicsRig) -> None:
    """EPICS says degC; UCUM says Cel. The table does the conversion."""
    temperature = introspect(device).component("epics_rig_temperature")
    assert temperature.egu == "degC"
    assert temperature.unit == "Cel"


def test_control_limits_are_adopted_from_the_ioc(device: EpicsRig) -> None:
    assert introspect(device).component("epics_rig_stage_x").limits == (-25.0, 25.0)


def test_an_epics_device_needs_no_annotations_at_all(device: EpicsRig) -> None:
    """Zero-config: when the IOC declares units, nothing has to be written."""
    draft = introspect(device)
    assert draft.is_complete
    assert draft.unresolved == []


def test_read_only_signals_are_not_given_actuation_commands(device: EpicsRig) -> None:
    commands = {c.name for c in introspect(device).commands}
    assert "set_stage_x" in commands
    assert "set_temperature" not in commands  # EpicsSignalRO: no write access


async def test_the_bridge_serves_a_real_epics_device_end_to_end(device: EpicsRig) -> None:
    """Discover, read, and actuate a Channel Access device through Labwire."""
    instrument = OphydInstrument(device, AnnotationFile())
    server = InstrumentServer(instrument, confirmation_token=GRANT)
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    client = LabwireClient.attach(client_end)
    await client.__aenter__()
    try:
        descriptor = await client.describe()
        channels = {c.name: c.unit for c in descriptor.channels}
        assert channels["epics_rig_stage_x"] == "mm"
        assert channels["epics_rig_temperature"] == "Cel"

        readings: dict[str, Any] = await (await client.submit("read", {})).result(timeout=30.0)
        assert readings["epics_rig_temperature"] == pytest.approx(21.5, abs=0.01)

        handle = await client.submit("set_stage_x", {"value": -3.25}, confirmation=GRANT)
        moved: dict[str, Any] = await handle.result(timeout=30.0)
        assert moved["value"] == pytest.approx(-3.25, abs=0.01)
        assert device.stage_x.get() == pytest.approx(-3.25, abs=0.01)
    finally:
        await client.close()
        await server.aclose()
