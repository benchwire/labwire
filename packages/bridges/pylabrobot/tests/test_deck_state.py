import json
from pathlib import Path

import pytest
from labwire.bridges.pylabrobot import (
    AnnotationError,
    AnnotationFile,
    LabwareKind,
    annotation_for,
    check,
    command_surface,
    deck_state,
    load_annotations,
    locked_labware,
    resolve,
)
from labwire.bridges.pylabrobot.annotations import ResourceAnnotation
from pylabrobot.liquid_handling import LiquidHandler

# --- the projection ---------------------------------------------------------


async def test_a_fresh_deck_reports_no_liquid_at_all(rig: LiquidHandler) -> None:
    """Sparse means a clean deck projects to nothing, not to 192 zeroes."""
    assert deck_state(rig).contents == []


async def test_a_well_with_liquid_appears_with_its_address(rig: LiquidHandler) -> None:
    resolve(rig, "source_plate/A1").tracker.set_volume(200.0)
    contents = deck_state(rig).contents
    assert len(contents) == 1
    assert contents[0].address == "source_plate/A1"
    assert contents[0].volume_ul == 200.0
    assert contents[0].max_volume_ul == 360.0


async def test_channels_report_whether_they_hold_a_tip(rig: LiquidHandler) -> None:
    before = deck_state(rig).channels
    assert len(before) == 8
    assert not any(channel.has_tip for channel in before)

    await rig.pick_up_tips(resolve(rig, "tips").get_items(["A1", "B1"]))
    after = deck_state(rig).channels
    assert [channel.has_tip for channel in after[:3]] == [True, True, False]
    assert after[0].tip_max_volume_ul == 1065.0  # bounds a single aspiration


async def test_a_tip_rack_reports_how_many_tips_are_left(rig: LiquidHandler) -> None:
    assert deck_state(rig).find("tips").tips_available == 96
    await rig.pick_up_tips(resolve(rig, "tips").get_items(["A1", "B1"]))
    assert deck_state(rig).find("tips").tips_available == 94


async def test_the_projection_stays_small_with_a_deck_in_use(rig: LiquidHandler) -> None:
    for row in "ABCDEFGH":
        resolve(rig, f"source_plate/{row}1").tracker.set_volume(300.0)
    projected = json.dumps(deck_state(rig).model_dump(mode="json"))
    assert len(json.dumps(rig.serialize())) > 100_000
    assert len(projected) < 8_000


async def test_tip_racks_do_not_report_well_contents(rig: LiquidHandler) -> None:
    """A tip spot is not a container; counting tips is the useful projection."""
    addresses = {well.address for well in deck_state(rig).contents}
    assert not any(address.startswith("tips/") for address in addresses)


# --- annotations ------------------------------------------------------------


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "labwire-pylabrobot.yaml"
    path.write_text(text)
    return path


def test_an_absent_file_is_reported_by_path(tmp_path: Path) -> None:
    with pytest.raises(AnnotationError, match="not found"):
        load_annotations(tmp_path / "missing.yaml")


def test_invalid_yaml_is_reported_by_path(tmp_path: Path) -> None:
    with pytest.raises(AnnotationError, match="invalid YAML"):
        load_annotations(_write(tmp_path, "resources: [unclosed\n"))


def test_a_non_mapping_top_level_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AnnotationError, match="mapping at the top level"):
        load_annotations(_write(tmp_path, "- a\n- b\n"))


def test_an_unknown_key_is_an_error_not_a_silent_no_op(tmp_path: Path) -> None:
    """A typo'd hazard annotation is the worst possible thing to ignore."""
    with pytest.raises(AnnotationError, match="hazzard"):
        load_annotations(_write(tmp_path, "resources:\n  acid: {hazzard: corrosive}\n"))


def test_an_unsupported_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AnnotationError, match="version 9"):
        load_annotations(_write(tmp_path, "version: 9\n"))


def test_a_full_file_loads(tmp_path: Path) -> None:
    annotations = load_annotations(
        _write(
            tmp_path,
            """
version: 1
instrument:
  description: A STARlet running dilutions.
  intent_tags: [liquid_handling]
commands:
  transfer: {estimated_duration_s: 4.0}
labware:
  Cor_96_wellplate_360ul_Fb: {description: A Costar 96-well plate.}
resources:
  source_plate:
    description: 1 M hydrochloric acid.
    hazard: corrosive
    safety_class: S3
    locked: true
""",
        )
    )
    assert annotations.instrument.intent_tags == ["liquid_handling"]
    assert annotations.commands["transfer"].estimated_duration_s == 4.0
    assert annotations.resources["source_plate"].hazard == "corrosive"


def test_resource_entries_override_labware_entries_field_by_field() -> None:
    annotations = AnnotationFile(
        labware={
            "Cor_96_wellplate_360ul_Fb": ResourceAnnotation(description="A plate.", hazard="none")
        },
        resources={"acid_stock": ResourceAnnotation(hazard="corrosive")},
    )
    merged = annotation_for(
        annotations, name="acid_stock", model="Cor_96_wellplate_360ul_Fb", type_name="Plate"
    )
    assert merged.hazard == "corrosive"  # the resource entry wins
    assert merged.description == "A plate."  # untouched fields survive


def test_an_unannotated_resource_gets_harmless_defaults() -> None:
    merged = annotation_for(AnnotationFile(), name="whatever")
    assert merged.locked is False
    assert merged.hazard is None


# --- annotations checked against the deck -----------------------------------


async def test_an_annotation_naming_a_resource_that_is_not_there_is_refused(
    rig: LiquidHandler,
) -> None:
    state = deck_state(rig)
    with pytest.raises(AnnotationError, match="acid_stock"):
        check(
            AnnotationFile(resources={"acid_stock": ResourceAnnotation(hazard="corrosive")}),
            known_resources={item.address for item in state.labware},
            known_labware={item.type_name for item in state.labware},
            known_commands={c.name for c in command_surface()},
        )


def test_an_annotation_naming_an_unexposed_command_is_refused() -> None:
    from labwire.bridges.pylabrobot.annotations import CommandAnnotation

    with pytest.raises(AnnotationError, match="move_plate"):
        check(
            AnnotationFile(commands={"move_plate": CommandAnnotation(exclude=True)}),
            known_resources=set(),
            known_labware=set(),
            known_commands={c.name for c in command_surface()},
        )


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(AnnotationError) as caught:
        check(
            AnnotationFile(
                resources={"nope": ResourceAnnotation(), "also_nope": ResourceAnnotation()}
            ),
            known_resources={"real"},
            known_labware=set(),
            known_commands=set(),
        )
    assert len(caught.value.problems) == 2


# --- hazards surfaced, and the one escalation that is real ------------------


async def test_a_hazard_annotation_reaches_the_deck_projection(rig: LiquidHandler) -> None:
    """An agent has to be able to see what it is about to pipette."""
    annotations = AnnotationFile(
        resources={
            "source_plate": ResourceAnnotation(hazard="corrosive", safety_class="S3"),
        }
    )
    plate = deck_state(rig, annotations).find("source_plate")
    assert plate.hazard == "corrosive"
    assert plate.safety_class == "S3"


async def test_locking_a_plate_locks_every_well_of_it(rig: LiquidHandler) -> None:
    """Locking is checked through the parent, so nobody names 96 wells."""
    annotations = AnnotationFile(resources={"source_plate": ResourceAnnotation(locked=True)})
    wells = [resolve(rig, "source_plate/A1"), resolve(rig, "source_plate/H12")]
    assert locked_labware(annotations, wells) == ["source_plate"]


async def test_an_unlocked_plate_reports_nothing_locked(rig: LiquidHandler) -> None:
    wells = [resolve(rig, "source_plate/A1")]
    assert locked_labware(AnnotationFile(), wells) == []


async def test_locking_by_labware_model_covers_every_instance(rig: LiquidHandler) -> None:
    annotations = AnnotationFile(
        labware={"Cor_96_wellplate_360ul_Fb": ResourceAnnotation(locked=True)}
    )
    wells = [resolve(rig, "source_plate/A1"), resolve(rig, "target_plate/A1")]
    assert sorted(locked_labware(annotations, wells)) == ["source_plate", "target_plate"]


async def test_labware_kinds_survive_annotation(rig: LiquidHandler) -> None:
    state = deck_state(rig, AnnotationFile())
    assert state.find("tips").kind is LabwareKind.TIP_RACK
    assert state.find("source_plate").kind is LabwareKind.PLATE
