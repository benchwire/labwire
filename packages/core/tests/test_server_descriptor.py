"""Tests for Instrument declaration: @command, channel(), interlock()."""

from labwire.core.capabilities import IdentityInfo
from labwire.core.server import CommandContext, Instrument, channel, command, interlock


class Pump(Instrument):
    """A minimal pump for descriptor tests."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="SimPump-100",
        serial_number="SIM-0001",
        firmware_version="0.1.0",
    )
    max_concurrent_commands = 2

    flow_rate = channel("flow_rate", unit="uL/min", description="Instantaneous flow rate.")
    over_pressure = interlock(
        "over_pressure", description="Trips on unsafe line pressure.", kind="hard"
    )

    @command(
        title="Dispense volume",
        units={"volume_ul": "uL", "rate_ul_min": "uL/min"},
        estimated_duration_s=30.0,
    )
    async def dispense(
        self, ctx: CommandContext, volume_ul: float, rate_ul_min: float = 100.0
    ) -> dict[str, float]:
        """Dispense a volume of liquid at a controlled flow rate."""
        return {"dispensed_ul": volume_ul}

    @command(interruptible=False, clears_interlocks=["over_pressure"])
    async def reset_pressure(self, ctx: CommandContext) -> dict[str, bool]:
        """Vent the line and clear the over-pressure interlock."""
        return {"cleared": True}


def test_descriptor_identity_and_concurrency() -> None:
    desc = Pump().describe()
    assert desc.identity.model == "SimPump-100"
    assert desc.max_concurrent_commands == 2


def test_descriptor_commands_carry_schema_units_and_metadata() -> None:
    desc = Pump().describe()
    by_name = {c.name: c for c in desc.commands}
    dispense = by_name["dispense"]
    assert dispense.title == "Dispense volume"
    assert dispense.description.startswith("Dispense a volume")
    assert dispense.params_schema["required"] == ["volume_ul"]
    assert dispense.params_schema["properties"]["rate_ul_min"]["default"] == 100.0
    assert dispense.unit_annotations == {"volume_ul": "uL", "rate_ul_min": "uL/min"}
    assert dispense.estimated_duration_s == 30.0
    assert dispense.interruptible is True
    reset = by_name["reset_pressure"]
    assert reset.interruptible is False
    assert reset.clears_interlocks == ["over_pressure"]


def test_descriptor_channels_and_interlocks() -> None:
    desc = Pump().describe()
    assert [c.name for c in desc.channels] == ["flow_rate"]
    assert desc.channels[0].unit == "uL/min"
    assert desc.channels[0].dtype == "float64"
    assert [i.name for i in desc.interlocks] == ["over_pressure"]
    assert desc.interlocks[0].tripped is False


def test_interlock_trip_state_reflected_in_descriptor() -> None:
    pump = Pump()
    pump.over_pressure.trip()
    assert pump.describe().interlocks[0].tripped is True
    pump.over_pressure.clear()
    assert pump.describe().interlocks[0].tripped is False


def test_instances_do_not_share_channel_or_interlock_state() -> None:
    a, b = Pump(), Pump()
    a.over_pressure.trip()
    assert b.describe().interlocks[0].tripped is False
