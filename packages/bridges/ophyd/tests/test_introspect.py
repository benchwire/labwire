"""Tests for the ophyd introspection layer (pure, no I/O)."""

import pytest
from labwire.bridges.ophyd.introspect import (
    ComponentRole,
    DraftInstrument,
    UnresolvedReason,
    introspect,
)
from ophyd import Component as Cpt
from ophyd import Device, Kind, Signal
from ophyd.sim import SynAxis, SynGauss


class UnitSignal(Signal):
    """A signal that reports units and control limits the way EpicsSignal does.

    ``ophyd.sim`` devices carry no ``units`` key at all, so this double is how
    the auto-adopt path is exercised without EPICS. It mirrors
    ``EpicsSignalBase.describe()`` exactly: units plus control limits.
    """

    def __init__(self, *args: object, egu: str = "mm", **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        self._egu = egu

    def describe(self) -> dict[str, dict[str, object]]:
        described = super().describe()
        described[self.name].update(units=self._egu, lower_ctrl_limit=-10.0, upper_ctrl_limit=10.0)
        return described


class Rig(Device):
    """One component per Kind, plus a units-bearing signal."""

    stage_x = Cpt(UnitSignal, value=0.0, kind=Kind.hinted)
    plain = Cpt(Signal, value=0.0, kind=Kind.normal)
    velocity = Cpt(UnitSignal, value=1.0, egu="mm/s", kind=Kind.config)
    ignored = Cpt(Signal, value=0, kind=Kind.omitted)


def test_identity_describes_the_device_not_a_vendor() -> None:
    draft = introspect(SynAxis(name="ax"))
    assert draft.identity.serial_number == "ax"
    assert draft.identity.model == "SynAxis"
    assert "ophyd" in draft.identity.manufacturer.lower()
    assert draft.identity.firmware_version  # the ophyd version in use


def test_hinted_and_normal_components_become_read_channels() -> None:
    draft = introspect(Rig(name="rig"))
    roles = {c.key: c.role for c in draft.components}
    assert roles["rig_stage_x"] is ComponentRole.CHANNEL
    assert roles["rig_plain"] is ComponentRole.CHANNEL
    assert roles["rig_velocity"] is ComponentRole.CONFIGURATION
    assert "rig_ignored" not in roles  # Kind.omitted is skipped entirely


def test_units_are_adopted_from_describe_when_present() -> None:
    draft = introspect(Rig(name="rig"))
    position = draft.component("rig_stage_x")
    assert position.unit == "mm"
    assert position.unit_source == "describe"
    velocity = draft.component("rig_velocity")
    assert velocity.unit == "mm/s"


def test_control_limits_are_adopted_but_zero_zero_is_not_a_limit() -> None:
    draft = introspect(Rig(name="rig"))
    assert draft.component("rig_stage_x").limits == (-10.0, 10.0)
    # ophyd reports (0, 0) for an unset limit pair; that is absence, not a bound
    assert draft.component("rig_plain").limits is None


def test_signals_without_units_are_reported_unresolved_not_guessed() -> None:
    draft = introspect(Rig(name="rig"))
    plain = draft.component("rig_plain")
    assert plain.unit is None
    assert plain.unit_source is None
    unresolved = {u.key: u.reason for u in draft.unresolved}
    assert unresolved["rig_plain"] is UnresolvedReason.NO_UNIT
    assert draft.is_complete is False


def test_sim_devices_have_no_units_at_all() -> None:
    """The empirical fact the design turns on: ophyd.sim carries no units."""
    draft = introspect(SynAxis(name="ax"))
    assert all(c.unit is None for c in draft.components)
    assert {u.key for u in draft.unresolved} == {c.key for c in draft.components}


def test_positioners_expose_move_rather_than_signal_setters() -> None:
    """A put to a setpoint signal returns before the motion completes."""
    draft = introspect(SynAxis(name="ax"))
    commands = {c.name: c for c in draft.commands}
    assert commands["move"].safety_class == "S2"
    assert commands["move"].component_key == "ax"  # the primary readback
    assert not any(c.name.startswith("set_") for c in draft.commands)


def test_non_positioners_expose_per_component_setters_at_s2() -> None:
    class Bank(Device):
        knob = Cpt(UnitSignal, value=0.0, kind=Kind.hinted)

    draft = introspect(Bank(name="bank"))
    commands = {c.name: c for c in draft.commands}
    assert "move" not in commands
    assert commands["set_knob"].safety_class == "S2"
    assert commands["set_knob"].component_key == "bank_knob"


def test_trigger_is_s1_stop_is_s0_read_is_s1() -> None:
    draft = introspect(
        SynGauss(name="det", motor=SynAxis(name="m"), motor_field="m", center=0, Imax=1)
    )
    classes = {c.name: c.safety_class for c in draft.commands}
    assert classes["trigger"] == "S1"
    assert classes["stop"] == "S0"
    assert classes["read"] == "S1"


def test_dtype_maps_to_labwire_dtypes() -> None:
    draft = introspect(Rig(name="rig"))
    assert draft.component("rig_stage_x").dtype == "float64"
    axis = introspect(SynAxis(name="ax"))
    # ophyd infers dtype from the current value, so an axis resting at int 0
    # reports "integer" — recorded faithfully, overridable by annotation later
    assert axis.component("ax").dtype in {"float64", "int64"}


def test_array_components_are_refused_with_a_reason() -> None:
    class ArrayRig(Device):
        image = Cpt(Signal, value=[1, 2, 3], kind=Kind.hinted)

    draft = introspect(ArrayRig(name="cam"))
    reasons = {u.key: u.reason for u in draft.unresolved}
    assert reasons["cam_image"] is UnresolvedReason.UNSUPPORTED_DTYPE
    assert draft.component("cam_image").role is ComponentRole.UNSUPPORTED


def test_introspection_is_pure_and_repeatable() -> None:
    device = Rig(name="rig")
    first, second = introspect(device), introspect(device)
    assert first.model_dump() == second.model_dump()
    assert isinstance(first, DraftInstrument)


def test_a_fully_annotated_device_needs_no_annotations() -> None:
    class Complete(Device):
        stage_x = Cpt(UnitSignal, value=0.0, kind=Kind.hinted)

    draft = introspect(Complete(name="c"))
    assert draft.is_complete is True
    assert draft.unresolved == []


def test_unresolved_entries_name_the_component_path_for_annotation() -> None:
    draft = introspect(Rig(name="rig"))
    entry = next(u for u in draft.unresolved if u.key == "rig_plain")
    assert entry.attr == "plain"
    assert entry.device_class.endswith("Rig")
    assert "plain" in entry.message


@pytest.mark.parametrize("egu", ["", "  ", None])
def test_blank_egu_is_not_a_unit(egu: str | None) -> None:
    class BlankUnits(Signal):
        def describe(self) -> dict[str, dict[str, object]]:
            described = super().describe()
            described[self.name].update(units=egu)
            return described

    class BlankRig(Device):
        v = Cpt(BlankUnits, value=0.0, kind=Kind.hinted)

    draft = introspect(BlankRig(name="b"))
    assert draft.component("b_v").unit is None
    assert any(u.key == "b_v" for u in draft.unresolved)
