import pytest
from labwire.bridges.pylabrobot import Address, address_of, resolve, resolve_all
from labwire.core.errors import ValidationError
from pylabrobot.liquid_handling import LiquidHandler

# --- the grammar ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "labware", "item"),
    [
        ("plate", "plate", None),
        ("source_plate/A1", "source_plate", "A1"),
        ("tips/H12", "tips", "H12"),
        ("plate-2/A1", "plate-2", "A1"),
        ("plate.left/B7", "plate.left", "B7"),
    ],
)
def test_well_formed_addresses_parse(text: str, labware: str, item: str | None) -> None:
    parsed = Address.parse(text)
    assert (parsed.labware, parsed.item) == (labware, item)
    assert str(parsed) == text  # round trips


@pytest.mark.parametrize(
    "text",
    [
        "",
        "/A1",  # no labware
        "plate/",  # trailing separator
        "plate/A1/B2",  # two levels
        "plate/A1:H1",  # PyLabRobot's range syntax is deliberately not accepted
        "plate A1",
        "_plate/A1",  # must start alphanumeric
        "plate/A 1",
    ],
)
def test_malformed_addresses_are_rejected_with_an_example(text: str) -> None:
    with pytest.raises(ValidationError, match="source_plate/A1"):
        Address.parse(text)


def test_a_non_string_is_rejected_rather_than_coerced() -> None:
    with pytest.raises(ValidationError, match="malformed address"):
        Address.parse(7)  # pyright: ignore[reportArgumentType]


# --- resolution -------------------------------------------------------------


async def test_labware_resolves_to_the_resource_itself(rig: LiquidHandler) -> None:
    assert resolve(rig, "source_plate").name == "source_plate"


async def test_an_item_resolves_to_the_well(rig: LiquidHandler) -> None:
    well = resolve(rig, "source_plate/A1")
    assert well.name == "source_plate_well_A1"
    assert well.max_volume == 360


async def test_a_tip_spot_resolves(rig: LiquidHandler) -> None:
    assert resolve(rig, "tips/A1").tracker.has_tip is True


async def test_the_last_item_of_the_grid_resolves(rig: LiquidHandler) -> None:
    """H12 is index 95 of a 96-well plate: the corner most likely to be off by one."""
    assert resolve(rig, "source_plate/H12").name == "source_plate_well_H12"


async def test_an_unknown_labware_error_lists_what_is_there(rig: LiquidHandler) -> None:
    with pytest.raises(ValidationError) as caught:
        resolve(rig, "nonexistent_plate")
    message = str(caught.value)
    assert "nonexistent_plate" in message
    assert "source_plate" in message  # what would have worked
    assert "tips" in message
    assert "source_plate_well_A1" not in message  # wells are not listed as labware


async def test_an_unknown_item_error_reports_the_grid_shape(rig: LiquidHandler) -> None:
    with pytest.raises(ValidationError) as caught:
        resolve(rig, "source_plate/Z99")
    assert "8 rows by 12 columns" in str(caught.value)


async def test_addressing_an_item_of_something_itemless_explains_itself(
    rig: LiquidHandler,
) -> None:
    with pytest.raises(ValidationError) as caught:
        resolve(rig, "trash/A1")
    message = str(caught.value)
    assert "no addressable items" in message
    assert "'trash'" in message  # tells you what to say instead


def test_an_unloaded_deck_still_lists_its_built_in_labware(bare_deck: object) -> None:
    """A STARlet deck is never truly empty: it ships with a trash and grippers."""
    with pytest.raises(ValidationError) as caught:
        resolve(bare_deck, "source_plate")
    assert "trash" in str(caught.value)


def test_a_deck_with_nothing_at_all_says_none_assigned() -> None:
    from pylabrobot.resources import Deck

    empty = Deck(name="empty", size_x=500.0, size_y=500.0, size_z=100.0)
    with pytest.raises(ValidationError, match=r"none assigned"):
        resolve(empty, "source_plate")


async def test_pylabrobots_own_derived_names_are_refused_with_the_canonical_one(
    rig: LiquidHandler,
) -> None:
    """PyLabRobot resolves derived names; the bridge refuses them, helpfully.

    ``get_resource`` searches the whole subtree, so ``source_plate_well_A1``
    would otherwise be a second spelling of ``source_plate/A1``.
    """
    with pytest.raises(ValidationError) as caught:
        resolve(rig, "source_plate_well_A1")
    assert "'source_plate/A1'" in str(caught.value)  # says what to use instead


async def test_resolve_all_preserves_order(rig: LiquidHandler) -> None:
    wells = resolve_all(rig, ["source_plate/B1", "source_plate/A1"])
    assert [w.name for w in wells] == ["source_plate_well_B1", "source_plate_well_A1"]


async def test_resolve_all_fails_on_the_first_bad_address(rig: LiquidHandler) -> None:
    with pytest.raises(ValidationError, match="Z99"):
        resolve_all(rig, ["source_plate/A1", "source_plate/Z99", "source_plate/B1"])


# --- the reverse direction --------------------------------------------------


async def test_address_of_a_well_is_the_address_that_resolves_back_to_it(
    rig: LiquidHandler,
) -> None:
    well = resolve(rig, "source_plate/C4")
    assert address_of(well) == "source_plate/C4"
    assert resolve(rig, address_of(well)) is well


async def test_address_of_labware_is_its_name(rig: LiquidHandler) -> None:
    assert address_of(resolve(rig, "tips")) == "tips"


async def test_every_well_of_a_plate_round_trips(rig: LiquidHandler) -> None:
    """The address grammar has to hold for all 96, not just the corners."""
    plate = resolve(rig, "source_plate")
    for well in plate.get_all_items():
        assert resolve(rig, address_of(well)) is well
