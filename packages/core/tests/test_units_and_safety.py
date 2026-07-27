"""Tests for v0.2: mandatory UCUM units and S0-S3 safety classes (SPEC §7, §8.6)."""

from collections.abc import AsyncIterator
from typing import TypedDict

import pytest
from labwire.core import (
    ChannelSpec,
    CommandContext,
    CommandSpec,
    ConfirmationRequiredError,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    InterlockError,
    LabwireClient,
    MemoryTransport,
    channel,
    command,
    interlock,
)
from pydantic import ConfigDict

GRANT = "operator-grant-under-test"

_IDENTITY = IdentityInfo(
    manufacturer="Labwire Project",
    model="SafetyRig-1",
    serial_number="SIM-0031",
    firmware_version="0.2.0",
)


class ReadResult(TypedDict):
    """A closed result schema for this test instrument."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]

    read_ml: float


class ConsumeResult(TypedDict):
    """A closed result schema for this test instrument."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]

    consumed_ml: float


class IrradiateResult(TypedDict):
    """A closed result schema for this test instrument."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]

    delivered_j: float


class PressureResult(TypedDict):
    """A closed result schema for this test instrument."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]

    pressure: float


class RatioResult(TypedDict):
    """A closed result schema for this test instrument."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]

    ratio: float


class SafetyRig(Instrument):
    """One command per safety class, plus a recovery path."""

    identity = _IDENTITY
    level = channel("level", unit="mL", description="Vessel level.", qudt_quantity_kind="Volume")
    spill = interlock("spill", description="Vessel overflowed; cleared by vent.", kind="soft")

    @command(returns_units={"read_ml": "mL"})
    async def read(self, ctx: CommandContext) -> ReadResult:
        """Read the level (routine, reversible)."""
        return {"read_ml": 1.0}

    @command(units={"volume_ml": "mL"}, returns_units={"consumed_ml": "mL"}, safety_class="S2")
    async def consume(self, ctx: CommandContext, volume_ml: float) -> ConsumeResult:
        """Consume reagent (irreversible)."""
        return {"consumed_ml": volume_ml}

    @command(units={"joules": "J"}, returns_units={"delivered_j": "J"}, safety_class="S3")
    async def irradiate(self, ctx: CommandContext, joules: float) -> IrradiateResult:
        """Fire the laser (hazardous)."""
        return {"delivered_j": joules}

    @command(safety_class="S0", clears_interlocks=["spill"])
    async def vent(self, ctx: CommandContext) -> dict[str, bool]:
        """Vent the vessel (emergency; always permitted)."""
        self.spill.clear()
        return {"vented": True}

    @command(safety_class="S0")
    async def estop(self, ctx: CommandContext) -> dict[str, bool]:
        """Emergency stop (always permitted, even while interlocked)."""
        return {"stopped": True}


async def _connect(**server_kwargs: object) -> tuple[SafetyRig, InstrumentServer, LabwireClient]:
    rig = SafetyRig()
    server = InstrumentServer(rig, **server_kwargs)  # pyright: ignore[reportArgumentType]
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    client = LabwireClient.attach(client_end)
    await client.__aenter__()
    return rig, server, client


@pytest.fixture
async def rig() -> AsyncIterator[tuple[SafetyRig, InstrumentServer, LabwireClient]]:
    instrument, server, client = await _connect(confirmation_token=GRANT)
    yield instrument, server, client
    await client.close()
    await server.aclose()


# --- units: declaration-time enforcement ----------------------------------


def _declare_unitless_param() -> type[Instrument]:
    class Bad(Instrument):
        identity = _IDENTITY

        @command()
        async def move(self, ctx: CommandContext, millimetres: float) -> None:
            """Move without declaring a unit."""

    return Bad


def _declare_unitless_result() -> type[Instrument]:
    class Bad(Instrument):
        identity = _IDENTITY

        @command()
        async def read_pressure(self, ctx: CommandContext) -> PressureResult:
            """Return a number with no declared result unit."""
            return {"pressure": 1.0}

    return Bad


def _declare_unitless_optional_param() -> type[Instrument]:
    class Bad(Instrument):
        identity = _IDENTITY

        @command()
        async def hold(self, ctx: CommandContext, seconds: float | None = None) -> None:
            """Optional numbers are quantities too."""

    return Bad


def test_numeric_param_without_unit_is_a_declaration_error() -> None:
    with pytest.raises(TypeError, match="lack UCUM unit codes"):
        _declare_unitless_param()


def test_numeric_result_without_unit_is_a_declaration_error() -> None:
    # A mapping return names no field, so the message says so rather than
    # naming one; either way the declaration is refused.
    with pytest.raises(TypeError, match=r"name no field|result field"):
        _declare_unitless_result()


def test_dimensionless_unity_code_satisfies_the_requirement() -> None:
    class Fine(Instrument):
        identity = _IDENTITY

        @command(units={"ratio": "1"}, returns_units={"ratio": "1"})
        async def set_ratio(self, ctx: CommandContext, ratio: float) -> RatioResult:
            """Set a dimensionless ratio."""
            return {"ratio": ratio}

    assert Fine().describe().commands[0].unit_annotations == {"ratio": "1"}


def test_string_parameters_need_no_unit() -> None:
    class Fine(Instrument):
        identity = _IDENTITY

        @command()
        async def label(self, ctx: CommandContext, text: str) -> None:
            """Label the sample; text is not a quantity."""

    assert Fine().describe().commands[0].unit_annotations == {}


# --- units: wire-level enforcement ----------------------------------------


def test_descriptor_from_the_wire_is_rejected_when_units_are_missing() -> None:
    """A client must refuse an under-annotated descriptor, not guess (SPEC §7.2)."""
    with pytest.raises(ValueError, match="lack UCUM unit codes"):
        CommandSpec.model_validate(
            {
                "name": "dose",
                "title": "Dose",
                "description": "Dose without units.",
                "params_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"ml": {"type": "number"}},
                },
                "unit_annotations": {},
                "interruptible": True,
            }
        )


def test_empty_unit_string_is_not_a_unit() -> None:
    with pytest.raises(ValueError, match="lack UCUM unit codes"):
        CommandSpec.model_validate(
            {
                "name": "dose",
                "title": "Dose",
                "description": "Dose with a blank unit.",
                "params_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"ml": {"type": "number"}},
                },
                "unit_annotations": {"ml": "  "},
                "interruptible": True,
            }
        )


def test_channel_requires_a_non_empty_unit_code() -> None:
    with pytest.raises(ValueError, match="non-empty UCUM code"):
        ChannelSpec.model_validate(
            {"name": "c", "description": "d", "dtype": "float64", "unit": ""}
        )


def test_optional_numeric_param_still_requires_a_unit() -> None:
    with pytest.raises(TypeError, match="lack UCUM unit codes"):
        _declare_unitless_optional_param()


# --- safety classes -------------------------------------------------------


async def test_descriptor_surfaces_safety_classes(
    rig: tuple[SafetyRig, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = rig
    classes = {c.name: c.safety_class for c in (await client.describe()).commands}
    assert classes == {
        "read": "S1",
        "consume": "S2",
        "irradiate": "S3",
        "vent": "S0",
        "estop": "S0",
    }


async def test_s1_needs_no_confirmation(
    rig: tuple[SafetyRig, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = rig
    handle = await client.submit("read", {})
    assert (await handle.result(timeout=5.0)) == {"read_ml": 1.0}


async def test_s2_without_confirmation_is_rejected(
    rig: tuple[SafetyRig, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = rig
    with pytest.raises(ConfirmationRequiredError) as excinfo:
        await client.submit("consume", {"volume_ml": 1.0})
    error = excinfo.value
    assert error.code == -32009
    assert error.retryable is False
    assert error.details == {"safety_class": "S2"}


async def test_s3_without_confirmation_is_rejected(
    rig: tuple[SafetyRig, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = rig
    with pytest.raises(ConfirmationRequiredError) as excinfo:
        await client.submit("irradiate", {"joules": 5.0})
    assert excinfo.value.details == {"safety_class": "S3"}


async def test_s2_with_the_configured_token_runs(
    rig: tuple[SafetyRig, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = rig
    handle = await client.submit("consume", {"volume_ml": 2.5}, confirmation=GRANT)
    assert (await handle.result(timeout=5.0)) == {"consumed_ml": 2.5}


async def test_wrong_token_is_rejected(
    rig: tuple[SafetyRig, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = rig
    with pytest.raises(ConfirmationRequiredError):
        await client.submit("consume", {"volume_ml": 1.0}, confirmation="guessed")


async def test_blank_confirmation_is_not_a_confirmation() -> None:
    _instrument, server, client = await _connect()  # no token configured
    try:
        with pytest.raises(ConfirmationRequiredError):
            await client.submit("consume", {"volume_ml": 1.0}, confirmation="   ")
        # with no token configured, any non-empty value is accepted policy (SPEC §8.6)
        handle = await client.submit("consume", {"volume_ml": 1.0}, confirmation="ack")
        assert (await handle.result(timeout=5.0))["consumed_ml"] == 1.0
    finally:
        await client.close()
        await server.aclose()


async def test_validation_precedes_confirmation(
    rig: tuple[SafetyRig, InstrumentServer, LabwireClient],
) -> None:
    """An unconfirmable request that could never run fails as validation (SPEC §12.1)."""
    from labwire.core import ValidationError

    with pytest.raises(ValidationError):
        await client_submit_bad(rig)


async def client_submit_bad(rig: tuple[SafetyRig, InstrumentServer, LabwireClient]) -> None:
    _instrument, _server, client = rig
    await client.submit("consume", {"volume_ml": "not a number"})


async def test_s0_stays_submittable_while_interlocked(
    rig: tuple[SafetyRig, InstrumentServer, LabwireClient],
) -> None:
    instrument, _server, client = rig
    instrument.spill.trip()
    with pytest.raises(InterlockError):  # ordinary commands blocked
        await client.submit("read", {})
    stop = await client.submit("estop", {})  # S0 without clears_interlocks
    assert (await stop.result(timeout=5.0)) == {"stopped": True}
    vent = await client.submit("vent", {})  # S0 recovery path
    assert (await vent.result(timeout=5.0)) == {"vented": True}
    ok = await client.submit("read", {})  # interlock cleared
    assert (await ok.result(timeout=5.0)) == {"read_ml": 1.0}


async def test_manifest_records_the_enforced_safety_class(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    import json

    runs = tmp_path_factory.mktemp("runs")
    _instrument, server, client = await _connect(confirmation_token=GRANT, manifest_dir=runs)
    try:
        handle = await client.submit("consume", {"volume_ml": 1.0}, confirmation=GRANT)
        await handle.result(timeout=5.0)
        doc = json.loads((runs / handle.command_id / "manifest.json").read_text())
        assert doc["command"]["safety_class"] == "S2"
        assert server.run_records[handle.command_id].safety_class == "S2"
        from labwire.core import verify_bundle

        assert verify_bundle(runs / handle.command_id).ok
    finally:
        await client.close()
        await server.aclose()
