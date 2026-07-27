import pytest
from labwire.bridges.pylabrobot import resolve, resolve_all, split_deck_uri, uri_of
from labwire.core.errors import ValidationError
from pylabrobot.liquid_handling import LiquidHandler

# --- the URI grammar (SPEC 10.1: one protocol-defined composition rule) -----


@pytest.mark.parametrize(
    ("uri", "labware", "item"),
    [
        ("labwire:deck/plate", "plate", None),
        ("labwire:deck/source_plate/A1", "source_plate", "A1"),
        ("labwire:deck/tips/H12", "tips", "H12"),
        ("labwire:deck/plate-2/A1", "plate-2", "A1"),
    ],
)
def test_well_formed_uris_split(uri: str, labware: str, item: str | None) -> None:
    assert split_deck_uri(uri) == (labware, item)


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "labwire:deck",  # the resource itself is not an item reference
        "labwire:deck/",  # empty segment
        "labwire:deck/plate/",  # trailing separator
        "labwire:deck/plate/A1/B2",  # too deep
        "labwire:deck//A1",  # empty labware
        "plate/A1",  # the pre-v0.3 bridge-private grammar is gone
        "source_plate",
        "labwire:freezer/plate",  # a resource this bridge does not declare
    ],
)
def test_malformed_uris_are_rejected_with_an_example(uri: str) -> None:
    with pytest.raises(ValidationError, match="labwire:deck/source_plate/A1"):
        split_deck_uri(uri)


# --- resolution -------------------------------------------------------------


async def test_labware_resolves_to_the_resource_itself(rig: LiquidHandler) -> None:
    assert resolve(rig, "labwire:deck/source_plate").name == "source_plate"


async def test_an_item_resolves_to_the_well(rig: LiquidHandler) -> None:
    well = resolve(rig, "labwire:deck/source_plate/A1")
    assert well.name == "source_plate_well_A1"
    assert well.max_volume == 360


async def test_a_tip_spot_resolves(rig: LiquidHandler) -> None:
    assert resolve(rig, "labwire:deck/tips/A1").tracker.has_tip is True


async def test_the_last_item_of_the_grid_resolves(rig: LiquidHandler) -> None:
    """H12 is index 95 of a 96-well plate: the corner most likely to be off by one."""
    assert resolve(rig, "labwire:deck/source_plate/H12").name == "source_plate_well_H12"


async def test_an_unknown_labware_error_lists_what_is_there(rig: LiquidHandler) -> None:
    with pytest.raises(ValidationError) as caught:
        resolve(rig, "labwire:deck/nonexistent_plate")
    message = str(caught.value)
    assert "nonexistent_plate" in message
    assert "labwire:deck/source_plate" in message  # what would have worked
    assert "labwire:deck/tips" in message
    assert "source_plate_well_A1" not in message  # wells are not listed as labware


async def test_an_unknown_item_error_reports_the_grid_shape(rig: LiquidHandler) -> None:
    with pytest.raises(ValidationError) as caught:
        resolve(rig, "labwire:deck/source_plate/Z99")
    assert "8 rows by 12 columns" in str(caught.value)


async def test_addressing_an_item_of_something_itemless_explains_itself(
    rig: LiquidHandler,
) -> None:
    with pytest.raises(ValidationError) as caught:
        resolve(rig, "labwire:deck/trash/A1")
    message = str(caught.value)
    assert "no addressable items" in message
    assert "labwire:deck/trash" in message  # tells you what to say instead


async def test_pylabrobots_own_derived_names_are_refused_with_the_canonical_uri(
    rig: LiquidHandler,
) -> None:
    """PyLabRobot resolves derived names; the bridge refuses them, helpfully.

    ``get_resource`` searches the whole subtree, so ``source_plate_well_A1``
    would otherwise be a second spelling of ``.../source_plate/A1``.
    """
    with pytest.raises(ValidationError) as caught:
        resolve(rig, "labwire:deck/source_plate_well_A1")
    assert "'labwire:deck/source_plate/A1'" in str(caught.value)


async def test_resolve_all_preserves_order(rig: LiquidHandler) -> None:
    wells = resolve_all(rig, ["labwire:deck/source_plate/B1", "labwire:deck/source_plate/A1"])
    assert [w.name for w in wells] == ["source_plate_well_B1", "source_plate_well_A1"]


async def test_resolve_all_fails_on_the_first_bad_uri(rig: LiquidHandler) -> None:
    with pytest.raises(ValidationError, match="Z99"):
        resolve_all(
            rig,
            [
                "labwire:deck/source_plate/A1",
                "labwire:deck/source_plate/Z99",
                "labwire:deck/source_plate/B1",
            ],
        )


# --- the reverse direction --------------------------------------------------


async def test_uri_of_a_well_is_the_uri_that_resolves_back_to_it(
    rig: LiquidHandler,
) -> None:
    well = resolve(rig, "labwire:deck/source_plate/C4")
    assert uri_of(well) == "labwire:deck/source_plate/C4"
    assert resolve(rig, uri_of(well)) is well


async def test_uri_of_labware_composes_from_its_name(rig: LiquidHandler) -> None:
    assert uri_of(resolve(rig, "labwire:deck/tips")) == "labwire:deck/tips"


async def test_every_well_of_a_plate_round_trips(rig: LiquidHandler) -> None:
    """The composition rule has to hold for all 96, not just the corners."""
    plate = resolve(rig, "labwire:deck/source_plate")
    for well in plate.get_all_items():
        assert resolve(rig, uri_of(well)) is well
