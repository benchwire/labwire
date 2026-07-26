"""Tests for the annotation file: schema, merge semantics, and resolution."""

from pathlib import Path

import pytest
from labwire.bridges.ophyd.annotations import (
    AnnotationError,
    AnnotationFile,
    load_annotations,
    resolve,
)
from labwire.bridges.ophyd.introspect import introspect
from ophyd import Component as Cpt
from ophyd import Device, Kind, Signal
from ophyd.sim import SynAxis

MODULE = __name__  # pytest imports this module by basename, not by package path


class UnitSignal(Signal):
    """Reports units the way EpicsSignal does (ophyd.sim reports none)."""

    def __init__(self, *args: object, egu: str = "mm", **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._egu = egu

    def describe(self) -> dict[str, dict[str, object]]:
        described = super().describe()
        described[self.name].update(units=self._egu, lower_ctrl_limit=-50.0, upper_ctrl_limit=50.0)
        return described


class BaseStage(Device):
    """A base class, so MRO inheritance of annotations can be tested."""

    stage_x = Cpt(Signal, value=0.0, kind=Kind.hinted)


class PreciseStage(BaseStage):
    """A subclass that adds one component."""

    fine_x = Cpt(Signal, value=0.0, kind=Kind.hinted)


class UnitStage(Device):
    """A device whose signal reports its own units and control limits."""

    stage_x = Cpt(UnitSignal, value=0.0, kind=Kind.hinted)


def _yaml(tmp_path: Path, text: str) -> Path:
    """Write an annotation file, substituting MOD for this test module's name."""
    path = tmp_path / "labwire-ophyd.yaml"
    path.write_text(text.replace("MOD", MODULE))
    return path


def _axis_units() -> dict[str, object]:
    """Annotations that close every gap on a SynAxis."""
    return {
        "version": 1,
        "devices": {
            "ophyd.sim.SynAxis": {
                "components": {
                    "readback": {"unit": "mm"},
                    "setpoint": {"unit": "mm"},
                    "velocity": {"unit": "mm/s"},
                    "acceleration": {"unit": "mm/s2"},
                }
            }
        },
    }


# --- schema and loading ----------------------------------------------------


def test_loads_a_minimal_file(tmp_path: Path) -> None:
    path = _yaml(
        tmp_path,
        """
version: 1
devices:
  MOD.BaseStage:
    components:
      stage_x: {unit: mm}
""",
    )
    annotations = load_annotations(path)
    assert isinstance(annotations, AnnotationFile)
    assert annotations.version == 1


def test_unknown_keys_are_rejected_so_typos_cannot_no_op(tmp_path: Path) -> None:
    path = _yaml(
        tmp_path,
        """
version: 1
devices:
  MOD.BaseStage:
    components:
      stage_x: {untis: mm}
""",
    )
    with pytest.raises(AnnotationError, match="untis"):
        load_annotations(path)


def test_unknown_safety_class_is_rejected(tmp_path: Path) -> None:
    path = _yaml(
        tmp_path,
        """
version: 1
devices:
  MOD.BaseStage:
    components:
      stage_x: {unit: mm, safety_class: S9}
""",
    )
    with pytest.raises(AnnotationError, match="S9"):
        load_annotations(path)


def test_unsupported_file_version_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AnnotationError, match="version"):
        load_annotations(_yaml(tmp_path, "version: 99\ndevices: {}\n"))


def test_missing_file_reports_the_path(tmp_path: Path) -> None:
    with pytest.raises(AnnotationError, match=r"nope\.yaml"):
        load_annotations(tmp_path / "nope.yaml")


def test_inverted_limits_are_rejected(tmp_path: Path) -> None:
    path = _yaml(
        tmp_path,
        """
version: 1
devices:
  MOD.BaseStage:
    components:
      stage_x: {unit: mm, limits: {low: 5.0, high: -5.0}}
""",
    )
    with pytest.raises(AnnotationError, match="below high"):
        load_annotations(path)


# --- resolution: the happy path --------------------------------------------


def test_annotations_close_the_gaps_the_draft_reported(tmp_path: Path) -> None:
    draft = introspect(BaseStage(name="stage"))
    assert not draft.is_complete  # ophyd.sim reports no units
    annotations = load_annotations(
        _yaml(
            tmp_path,
            """
version: 1
devices:
  MOD.BaseStage:
    description: A test stage.
    intent_tags: [motion]
    components:
      stage_x:
        unit: mm
        qudt_quantity_kind: Length
        description: Stage position.
""",
        )
    )
    resolved = resolve(draft, annotations)
    component = resolved.component("stage_stage_x")
    assert component.unit == "mm"
    assert component.qudt_quantity_kind == "Length"
    assert component.description == "Stage position."
    assert resolved.description == "A test stage."
    assert resolved.intent_tags == ["motion"]


def test_a_device_needing_nothing_resolves_without_annotations() -> None:
    resolved = resolve(introspect(UnitStage(name="c")), AnnotationFile())
    assert resolved.component("c_stage_x").unit == "mm"


# --- resolution: failure is loud and precise -------------------------------


def test_missing_unit_names_the_exact_component() -> None:
    with pytest.raises(AnnotationError) as excinfo:
        resolve(introspect(BaseStage(name="stage")), AnnotationFile())
    message = str(excinfo.value)
    assert "stage_stage_x" in message
    assert "BaseStage" in message
    assert "unit" in message


def test_every_gap_is_reported_at_once() -> None:
    with pytest.raises(AnnotationError) as excinfo:
        resolve(introspect(PreciseStage(name="stage")), AnnotationFile())
    assert len(excinfo.value.problems) == 2


def test_allow_partial_omits_the_gap_and_records_it(tmp_path: Path) -> None:
    annotations = load_annotations(
        _yaml(
            tmp_path,
            """
version: 1
devices:
  MOD.PreciseStage:
    components:
      fine_x: {unit: um}
""",
        )
    )
    resolved = resolve(introspect(PreciseStage(name="stage")), annotations, allow_partial=True)
    assert resolved.component("stage_fine_x").unit == "um"
    assert [o.key for o in resolved.omitted] == ["stage_stage_x"]
    with pytest.raises(KeyError):
        resolved.component("stage_stage_x")


def test_annotating_an_unknown_component_is_an_error(tmp_path: Path) -> None:
    """A typo'd component name must not silently annotate nothing."""
    annotations = load_annotations(
        _yaml(
            tmp_path,
            """
version: 1
devices:
  MOD.BaseStage:
    components:
      stage_x: {unit: mm}
      stage_y: {unit: mm}
""",
        )
    )
    with pytest.raises(AnnotationError, match="stage_y"):
        resolve(introspect(BaseStage(name="stage")), annotations)


# --- merge semantics -------------------------------------------------------


def test_subclass_inherits_base_class_annotations(tmp_path: Path) -> None:
    annotations = load_annotations(
        _yaml(
            tmp_path,
            """
version: 1
devices:
  MOD.BaseStage:
    components:
      stage_x: {unit: mm, description: Inherited.}
  MOD.PreciseStage:
    components:
      fine_x: {unit: um}
""",
        )
    )
    resolved = resolve(introspect(PreciseStage(name="stage")), annotations)
    assert resolved.component("stage_stage_x").unit == "mm"
    assert resolved.component("stage_stage_x").description == "Inherited."
    assert resolved.component("stage_fine_x").unit == "um"


def test_subclass_entry_overrides_base_entry_per_field(tmp_path: Path) -> None:
    annotations = load_annotations(
        _yaml(
            tmp_path,
            """
version: 1
devices:
  MOD.BaseStage:
    components:
      stage_x: {unit: mm, description: From the base.}
  MOD.PreciseStage:
    components:
      stage_x: {unit: um}
      fine_x: {unit: um}
""",
        )
    )
    component = resolve(introspect(PreciseStage(name="stage")), annotations).component(
        "stage_stage_x"
    )
    assert component.unit == "um"  # the subclass wins
    assert component.description == "From the base."  # an untouched field survives


def test_instance_entry_overrides_class_entry(tmp_path: Path) -> None:
    annotations = load_annotations(
        _yaml(
            tmp_path,
            """
version: 1
devices:
  MOD.BaseStage:
    components:
      stage_x: {unit: mm, limits: {low: -50.0, high: 50.0}}
instances:
  stage:
    components:
      stage_x: {limits: {low: -5.0, high: 5.0}}
""",
        )
    )
    component = resolve(introspect(BaseStage(name="stage")), annotations).component("stage_stage_x")
    assert component.unit == "mm"  # inherited from the class entry
    assert component.limits == (-5.0, 5.0)  # this instance is mechanically shorter


def test_instance_annotations_do_not_leak_to_other_instances(tmp_path: Path) -> None:
    annotations = load_annotations(
        _yaml(
            tmp_path,
            """
version: 1
devices:
  MOD.BaseStage:
    components:
      stage_x: {unit: mm}
instances:
  left:
    components:
      stage_x: {description: The left stage.}
""",
        )
    )
    left = resolve(introspect(BaseStage(name="left")), annotations).component("left_stage_x")
    right = resolve(introspect(BaseStage(name="right")), annotations).component("right_stage_x")
    assert left.description == "The left stage."
    assert right.description != "The left stage."


# --- limits and safety -----------------------------------------------------


def test_annotated_limits_intersect_with_reported_control_limits() -> None:
    annotations = AnnotationFile.model_validate(
        {
            "version": 1,
            "devices": {
                f"{MODULE}.UnitStage": {
                    "components": {"stage_x": {"limits": {"low": -10.0, "high": 80.0}}}
                }
            },
        }
    )
    component = resolve(introspect(UnitStage(name="s")), annotations).component("s_stage_x")
    # tightest bound on each side wins: annotated -10 low, device-reported 50 high
    assert component.limits == (-10.0, 50.0)


def test_safety_class_defaults_hold_without_annotation() -> None:
    resolved = resolve(introspect(SynAxis(name="ax")), AnnotationFile.model_validate(_axis_units()))
    classes = {c.name: c.safety_class for c in resolved.commands}
    assert classes["move"] == "S2"
    assert classes["stop"] == "S0"
    assert classes["read"] == "S1"


def test_annotation_can_raise_and_lower_a_safety_class() -> None:
    raw = _axis_units()
    devices = raw["devices"]
    assert isinstance(devices, dict)
    devices["ophyd.sim.SynAxis"]["commands"] = {  # pyright: ignore[reportIndexIssue]
        "move": {"safety_class": "S3"},  # a hazardous axis
        "read": {"safety_class": "S0"},
    }
    resolved = resolve(introspect(SynAxis(name="ax")), AnnotationFile.model_validate(raw))
    classes = {c.name: c.safety_class for c in resolved.commands}
    assert classes["move"] == "S3"
    assert classes["read"] == "S0"


def test_component_safety_class_applies_to_its_command() -> None:
    """A positioner's move takes its class from the position component."""
    raw = _axis_units()
    devices = raw["devices"]
    assert isinstance(devices, dict)
    devices["ophyd.sim.SynAxis"]["components"]["readback"]["safety_class"] = "S3"  # pyright: ignore[reportIndexIssue]
    resolved = resolve(introspect(SynAxis(name="ax")), AnnotationFile.model_validate(raw))
    classes = {c.name: c.safety_class for c in resolved.commands}
    assert classes["move"] == "S3"


def test_annotating_an_unknown_command_is_an_error() -> None:
    raw = _axis_units()
    devices = raw["devices"]
    assert isinstance(devices, dict)
    devices["ophyd.sim.SynAxis"]["commands"] = {"fly": {"safety_class": "S1"}}  # pyright: ignore[reportIndexIssue]
    with pytest.raises(AnnotationError, match="fly"):
        resolve(introspect(SynAxis(name="ax")), AnnotationFile.model_validate(raw))


def test_excluded_components_are_dropped_deliberately(tmp_path: Path) -> None:
    annotations = load_annotations(
        _yaml(
            tmp_path,
            """
version: 1
devices:
  MOD.PreciseStage:
    components:
      stage_x: {unit: mm}
      fine_x: {exclude: true}
""",
        )
    )
    resolved = resolve(introspect(PreciseStage(name="stage")), annotations)
    assert [c.key for c in resolved.components] == ["stage_stage_x"]
    assert resolved.omitted == []  # excluded on purpose is not an unresolved gap


def test_excluding_a_component_drops_its_command(tmp_path: Path) -> None:
    raw = _axis_units()
    devices = raw["devices"]
    assert isinstance(devices, dict)
    devices["ophyd.sim.SynAxis"]["components"]["readback"] = {"exclude": True}  # pyright: ignore[reportIndexIssue]
    resolved = resolve(introspect(SynAxis(name="ax")), AnnotationFile.model_validate(raw))
    assert "move" not in {c.name for c in resolved.commands}


def test_dtype_can_be_overridden_because_ophyd_infers_it_from_a_value() -> None:
    raw = _axis_units()
    devices = raw["devices"]
    assert isinstance(devices, dict)
    devices["ophyd.sim.SynAxis"]["components"]["readback"]["dtype"] = "float64"  # pyright: ignore[reportIndexIssue]
    resolved = resolve(introspect(SynAxis(name="ax")), AnnotationFile.model_validate(raw))
    assert resolved.component("ax").dtype == "float64"
