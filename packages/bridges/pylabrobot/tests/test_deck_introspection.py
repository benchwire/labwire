import json

import pytest
from labwire.bridges.pylabrobot import LabwareKind, UnresolvedReason, command_surface, introspect
from labwire.bridges.pylabrobot.introspect import Grid
from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.resources import Coordinate, Cor_96_wellplate_360ul_Fb

# --- identity ---------------------------------------------------------------


async def test_identity_names_the_backend_and_the_handler(rig: LiquidHandler) -> None:
    identity = introspect(rig).identity
    assert identity.model == "LiquidHandlerChatterboxBackend"
    assert identity.serial_number == rig.name
    assert identity.firmware_version.startswith("pylabrobot ")
    assert "PyLabRobot" in identity.manufacturer


async def test_the_channel_count_comes_from_the_backend(rig: LiquidHandler) -> None:
    assert introspect(rig).channel_count == 8


async def test_channel_count_is_available_before_setup() -> None:
    """num_channels is a backend argument; the head trackers only exist after setup."""
    from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
    from pylabrobot.resources.hamilton import STARLetDeck

    unstarted = LiquidHandler(
        backend=LiquidHandlerChatterboxBackend(num_channels=4), deck=STARLetDeck()
    )
    assert introspect(unstarted).channel_count == 4


# --- the deck projection ----------------------------------------------------


async def test_assigned_labware_is_found_and_classified(rig: LiquidHandler) -> None:
    draft = introspect(rig)
    assert draft.find("labwire:deck/source_plate").kind is LabwareKind.PLATE
    assert draft.find("labwire:deck/tips").kind is LabwareKind.TIP_RACK
    assert draft.find("labwire:deck/trash").kind is LabwareKind.TRASH


async def test_a_plate_reports_its_grid_and_well_capacity(rig: LiquidHandler) -> None:
    grid = introspect(rig).find("labwire:deck/source_plate").grid
    assert grid is not None
    assert grid == Grid(rows=8, columns=12, item_max_volume_ul=360.0)
    assert grid.item_count == 96


async def test_labware_carries_its_pylabrobot_model(rig: LiquidHandler) -> None:
    assert introspect(rig).find("labwire:deck/source_plate").model == "Cor_96_wellplate_360ul_Fb"


async def test_labware_reports_where_it_is_and_how_big_it_is(rig: LiquidHandler) -> None:
    plate = introspect(rig).find("labwire:deck/source_plate")
    assert plate.location_mm is not None
    assert plate.location_mm[0] > 0
    assert plate.size_mm == (127.76, 85.48, 14.2)  # a standard SBS footprint


async def test_wells_are_not_listed_as_labware(rig: LiquidHandler) -> None:
    """The projection lists what you address, not all 208 resources on the deck."""
    addresses = {item.uri for item in introspect(rig).labware}
    assert "labwire:deck/source_plate" in addresses
    assert not any(address.count("/") > 1 for address in addresses)  # no wells
    assert len(addresses) < 15  # the raw tree has 200+ resources


async def test_the_projection_is_small_enough_to_give_an_agent(rig: LiquidHandler) -> None:
    """The raw PyLabRobot serialization of this deck is over 130 KB."""
    projected = json.dumps(introspect(rig).model_dump(mode="json"))
    raw = json.dumps(rig.serialize())
    assert len(raw) > 100_000
    assert len(projected) < 6_000


# --- gaps, reported rather than guessed at ----------------------------------


async def test_the_labware_a_user_loaded_introspects_cleanly(rig: LiquidHandler) -> None:
    """Nothing is reported against the plates and tips actually being used."""
    flagged = {gap.uri for gap in introspect(rig).unresolved}
    assert not (flagged & {"source_plate", "target_plate", "tips"})


async def test_even_a_stock_deck_has_furniture_the_bridge_cannot_classify(
    rig: LiquidHandler,
) -> None:
    """Honest result: a STARlet ships with fixtures PyLabRobot gives no category.

    So ``is_complete`` is False for a perfectly ordinary deck. The gaps are
    reported by name rather than papered over with a guessed kind.
    """
    draft = introspect(rig)
    assert not draft.is_complete
    assert {gap.uri for gap in draft.unresolved} == {
        "labwire:deck/waste_block",
        "labwire:deck/core_grippers",
    }
    assert all(g.reason is UnresolvedReason.UNKNOWN_KIND for g in draft.unresolved)


async def test_labware_with_no_location_is_reported() -> None:
    """A plate held but never placed cannot be planned around.

    A Hamilton deck refuses a location-less assignment outright, so this needs
    the generic ``Deck``, where the situation is reachable.
    """
    from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
    from pylabrobot.resources import Deck

    deck = Deck(name="plain", size_x=500.0, size_y=500.0, size_z=100.0)
    handler = LiquidHandler(backend=LiquidHandlerChatterboxBackend(num_channels=1), deck=deck)
    deck.assign_child_resource(Cor_96_wellplate_360ul_Fb(name="unplaced_plate"), location=None)

    gaps = [
        gap for gap in introspect(handler).unresolved if gap.uri == "labwire:deck/unplaced_plate"
    ]
    assert UnresolvedReason.NO_LOCATION in {gap.reason for gap in gaps}
    assert any("assign it before serving" in gap.message for gap in gaps)


async def test_unrecognized_labware_stays_addressable_and_is_flagged(rig: LiquidHandler) -> None:
    """The STARlet's waste block has no category PyLabRobot names."""
    draft = introspect(rig)
    block = draft.find("labwire:deck/waste_block")
    assert block.kind is LabwareKind.OTHER
    reasons = {g.reason for g in draft.unresolved if g.uri == "labwire:deck/waste_block"}
    assert UnresolvedReason.UNKNOWN_KIND in reasons


async def test_an_unknown_labware_lookup_raises(rig: LiquidHandler) -> None:
    draft = introspect(rig)
    with pytest.raises(KeyError, match="nope"):
        draft.find("nope")


# --- the command surface ----------------------------------------------------


def test_material_moving_commands_are_s2() -> None:
    classes = {c.name: c.safety_class for c in command_surface()}
    for name in ("aspirate", "dispense", "transfer", "pick_up_tips", "drop_tips"):
        assert classes[name] == "S2", name


def test_stop_is_s0_and_the_deck_is_not_a_command() -> None:
    """stop must stay submittable while an interlock is tripped (SPEC 8.6).

    describe_deck is gone: the deck is the labwire:deck resource now, read
    rather than commanded, so nothing marks it S-anything.
    """
    classes = {c.name: c.safety_class for c in command_surface()}
    assert classes["stop"] == "S0"
    assert "describe_deck" not in classes


def test_declaring_a_wells_contents_is_s1_because_it_moves_nothing() -> None:
    """S0-S3 grades physical consequence; this changes only what the machine believes."""
    classes = {c.name: c.safety_class for c in command_surface()}
    assert classes["set_well_volume"] == "S1"


def test_the_command_surface_has_nine_operations() -> None:
    """Ten in v0.2; describe_deck became the deck resource."""
    assert len(command_surface()) == 9


def test_the_untyped_backend_passthrough_is_not_exposed() -> None:
    """Every PyLabRobot operation takes **backend_kwargs straight to vendor firmware."""
    names = {c.name for c in command_surface()}
    assert not any("backend" in name or "kwargs" in name for name in names)


def test_gripper_moves_are_not_exposed_in_this_version() -> None:
    names = {c.name for c in command_surface()}
    assert not (names & {"move_plate", "move_lid", "move_resource"})


def test_every_command_has_a_description() -> None:
    assert all(c.description.strip() for c in command_surface())


async def test_the_command_surface_does_not_depend_on_the_deck(rig: LiquidHandler) -> None:
    """PyLabRobot's frontend is one class, so there is nothing to discover."""
    before = introspect(rig).commands
    rig.deck.assign_child_resource(
        Cor_96_wellplate_360ul_Fb(name="extra"), location=Coordinate(600, 200, 100)
    )
    assert introspect(rig).commands == before
