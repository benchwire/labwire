"""EXPERIMENTAL plain-class bridge, tested the way PR #1184 tests.

Everything here runs against a simulation of the Opentrons robot-server
command layer (``opentrons_sim.FlexServerSim``); no hardware behavior
is claimed. The whole module is skipped unless the pinned PR-head
install of PyLabRobot (which alone contains ``pylabrobot.opentrons``)
(or any install providing the same modules) is present; the
``plr-v1-flex-experimental`` CI job on this branch installs the pin,
and normal CI never sees these tests run. These tests live
in their own directory because the shipped tests' conftest imports
LiquidHandler at module scope, and the pinned PR-head snapshot cannot
import it (its v1b1 base has a broken legacy import chain; see
V1B1.md).
"""

import asyncio
import contextlib
import importlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

httpx = pytest.importorskip("httpx", reason="the simulated command layer is httpx-based")
from labwire.core import (  # noqa: E402
    InstrumentServer,
    LabwireClient,
    LabwireError,
    MemoryTransport,
)
from opentrons_sim import FlexServerSim  # noqa: E402

opentrons = pytest.importorskip(
    "pylabrobot.opentrons", reason="needs the PR #1184 pinned install (plr-v1 branch job)"
)
flex_deck_module = pytest.importorskip(
    "pylabrobot.resources.opentrons.flex_deck", reason="needs the PR #1184 FlexDeck"
)
flex_racks = pytest.importorskip(
    "pylabrobot.resources.opentrons.flex_tip_racks", reason="needs the PR #1184 tip racks"
)
plr_resources = importlib.import_module("pylabrobot.resources")

from labwire.bridges.pylabrobot.plainclass import OpentronsFlexInstrument  # noqa: E402

CONFIRMATION = "flex-operator-confirmation"


@pytest.fixture(autouse=True)
def _tracking() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Tracking on, exactly as the shipped bridge's conftest does."""
    plr_resources.set_tip_tracking(True)
    plr_resources.set_volume_tracking(True)
    yield
    plr_resources.set_tip_tracking(False)
    plr_resources.set_volume_tracking(False)


def _build_deck() -> Any:
    deck = flex_deck_module.FlexDeck(with_trash_bin=True)
    deck.assign_child_at_slot(flex_racks.flex_96_tiprack_1000ul(name="tips"), slot="C1")
    source = plr_resources.Cor_96_wellplate_360ul_Fb(name="source_plate")
    source.ot_load_name = "corning_96_wellplate_360ul_flat"
    deck.assign_child_at_slot(source, slot="D1")
    target = plr_resources.Cor_96_wellplate_360ul_Fb(name="target_plate")
    target.ot_load_name = "corning_96_wellplate_360ul_flat"
    deck.assign_child_at_slot(target, slot="D2")
    return deck


@pytest.fixture
async def flex_rig(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Any, FlexServerSim]]:
    """A set-up plain-class Flex talking to the command-layer simulation."""
    sim = FlexServerSim()
    robot_module = importlib.import_module("pylabrobot.opentrons.robot")
    real_client: Any = httpx.AsyncClient

    def patched_client(**kwargs: Any) -> Any:
        kwargs.pop("transport", None)
        return real_client(transport=sim.transport(), **kwargs)

    monkeypatch.setattr(robot_module.httpx, "AsyncClient", patched_client)
    flex = opentrons.OpentronsFlex(deck=_build_deck(), host="sim.invalid")
    await flex.setup()
    yield flex, sim
    with contextlib.suppress(Exception):
        await flex.stop()


@pytest.fixture
async def served(
    flex_rig: tuple[Any, FlexServerSim],
) -> AsyncIterator[tuple[Any, FlexServerSim, LabwireClient]]:
    """The experimental instrument served over the protocol."""
    flex, sim = flex_rig
    server = InstrumentServer(OpentronsFlexInstrument(flex), confirmation_token=CONFIRMATION)
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        yield flex, sim, client
    await server.aclose()


async def _call(client: LabwireClient, name: str, params: dict[str, Any]) -> Any:
    handle = await client.submit(name, params, confirmation=CONFIRMATION)
    return await handle.result(timeout=20.0)


# --- setup drove the real protocol against the simulation -------------------


async def test_setup_talks_the_robot_server_protocol(flex_rig: tuple[Any, FlexServerSim]) -> None:
    """The driver's own setup sequence ran against the simulated API."""
    flex, sim = flex_rig
    assert flex.robot_model == "OT-3 Standard (simulated)"
    assert flex.pipette is not None
    assert flex.pipette.pipette_id == "pipette-sim-1"
    assert [c for c, _ in sim.commands[:2]] == ["loadPipette", "home"]


# --- the descriptor ---------------------------------------------------------


async def test_descriptor_curates_the_flex_surface(
    served: tuple[Any, FlexServerSim, LabwireClient],
) -> None:
    """Commands, units, safety classes, and cancel semantics, all declared."""
    _, _, client = served
    descriptor = await client.describe()
    commands = {spec.name: spec for spec in descriptor.commands}
    assert set(commands) == {
        "pick_up_tips",
        "drop_tips",
        "discard_tips",
        "aspirate",
        "dispense",
        "transfer",
        "set_well_volume",
        "home",
        "stop",
    }
    assert "move_plate" not in commands  # the driver has no gripper surface
    assert commands["aspirate"].safety_class == "S2"
    assert commands["aspirate"].unit_annotations["volumes_ul"] == "uL"
    assert commands["aspirate"].cancel_semantics == "none"
    assert commands["transfer"].cancel_semantics == "between_steps"
    assert commands["discard_tips"].cancel_semantics == "between_steps"
    assert commands["stop"].safety_class == "S0"
    assert descriptor.identity.manufacturer.startswith("Opentrons")
    assert "never tested on hardware" in descriptor.identity.manufacturer
    assert descriptor.identity.model == "OT-3 Standard (simulated)"


# --- the deck resource and typed references ---------------------------------


async def test_deck_resource_projects_the_flex_deck(
    served: tuple[Any, FlexServerSim, LabwireClient],
) -> None:
    """The shipped projection modules work unchanged over a FlexDeck."""
    _, _, client = served
    snapshot = await client.read_resource("labwire:deck")
    uris = {entry.uri for entry in snapshot.index}
    assert {
        "labwire:deck/tips",
        "labwire:deck/source_plate",
        "labwire:deck/target_plate",
    } <= uris
    labware = {item["uri"]: item for item in snapshot.content["labware"]}
    assert labware["labwire:deck/tips"]["tips_available"] == 96
    assert labware["labwire:deck/source_plate"]["kind"] == "plate"


async def test_a_bad_well_reference_is_refused_before_the_robot_sees_it(
    served: tuple[Any, FlexServerSim, LabwireClient],
) -> None:
    """Typed references validate against the FlexDeck before anything runs."""
    _, sim, client = served
    before = len(sim.commands)
    with pytest.raises(LabwireError) as excinfo:
        await _call(
            client,
            "aspirate",
            {"wells": ["labwire:deck/no_such_plate/A1"], "volumes_ul": [10.0]},
        )
    assert "no_such_plate" in str(excinfo.value)
    assert len(sim.commands) == before  # nothing reached the robot


# --- the command layer, asserted precisely ----------------------------------


async def test_pick_up_tips_sends_the_flex_command(
    served: tuple[Any, FlexServerSim, LabwireClient],
) -> None:
    """One pickUpTip with the right labware and well, tracked in the tree."""
    _, sim, client = served
    result = await _call(
        client, "pick_up_tips", {"tip_spots": ["labwire:deck/tips/A1"], "channels": [0]}
    )
    assert result["channels_used"] == [0]
    (pick,) = sim.sent("pickUpTip")
    assert pick["wellName"] == "A1"
    assert pick["pipetteId"] == "pipette-sim-1"
    loads = sim.sent("loadLabware")
    assert any(load["loadName"] == "opentrons_flex_96_tiprack_1000ul" for load in loads)
    snapshot = await client.read_resource("labwire:deck")
    labware = {item["uri"]: item for item in snapshot.content["labware"]}
    assert labware["labwire:deck/tips"]["tips_available"] == 95
    assert snapshot.content["channels"][0]["has_tip"] is True


async def test_liquid_flows_and_the_record_matches(
    served: tuple[Any, FlexServerSim, LabwireClient],
) -> None:
    """Aspirate and dispense payloads carry volume and flow rate in uL, uL/s."""
    _, sim, client = served
    await _call(client, "pick_up_tips", {"tip_spots": ["labwire:deck/tips/A1"]})
    await _call(
        client, "set_well_volume", {"well": "labwire:deck/source_plate/A1", "volume_ul": 200.0}
    )
    await _call(
        client,
        "aspirate",
        {
            "wells": ["labwire:deck/source_plate/A1"],
            "volumes_ul": [50.0],
            "flow_rates_ul_s": [30.0],
        },
    )
    (aspirate,) = sim.sent("aspirate")
    assert aspirate["volume"] == 50.0
    assert aspirate["flowRate"] == 30.0
    await _call(
        client, "dispense", {"wells": ["labwire:deck/target_plate/B2"], "volumes_ul": [50.0]}
    )
    (dispense,) = sim.sent("dispense")
    assert dispense["wellName"] == "B2"
    snapshot = await client.read_resource("labwire:deck")
    volumes = {well["uri"]: well["volume_ul"] for well in snapshot.content["contents"]}
    assert volumes["labwire:deck/source_plate/A1"] == 150.0
    assert volumes["labwire:deck/target_plate/B2"] == 50.0


async def test_discard_tips_routes_through_the_movable_trash(
    served: tuple[Any, FlexServerSim, LabwireClient],
) -> None:
    """The trash path uses the addressable-area drop, as the driver does."""
    _, sim, client = served
    await _call(client, "pick_up_tips", {"tip_spots": ["labwire:deck/tips/A1"]})
    result = await _call(client, "discard_tips", {})
    assert result["channels_used"] == [0]
    (move,) = sim.sent("moveToAddressableAreaForDropTip")
    assert move["addressableAreaName"] == "movableTrashA3"
    assert len(sim.sent("dropTipInPlace")) == 1
    snapshot = await client.read_resource("labwire:deck")
    assert snapshot.content["channels"][0]["has_tip"] is False


async def test_discarding_nothing_is_a_refusal_not_a_no_op(
    served: tuple[Any, FlexServerSim, LabwireClient],
) -> None:
    """No mounted tips means an honest error."""
    _, _, client = served
    with pytest.raises(LabwireError, match="no tips are mounted"):
        await _call(client, "discard_tips", {})


# --- failure mapping --------------------------------------------------------


async def test_a_failed_robot_command_maps_to_a_hardware_fault(
    served: tuple[Any, FlexServerSim, LabwireClient],
) -> None:
    """The API's failed status surfaces as an honest hardware fault."""
    _, sim, client = served
    await _call(client, "pick_up_tips", {"tip_spots": ["labwire:deck/tips/A1"]})
    sim.fail_types.add("aspirate")
    with pytest.raises(LabwireError, match="simulated aspirate failure"):
        await _call(
            client,
            "aspirate",
            {"wells": ["labwire:deck/source_plate/A1"], "volumes_ul": [10.0]},
        )


# --- cancellation is settled at boundaries, never claimed mid-call ----------


async def test_transfer_cancel_settles_at_the_aspirate_boundary(
    served: tuple[Any, FlexServerSim, LabwireClient],
) -> None:
    """A cancel during the held aspirate finishes it and stops at the boundary.

    Same shape as the shipped bridge's F10 test, but the hold is in the
    simulated robot-server (the command stays ``running`` until released),
    so the window is opened at the HTTP layer the real robot would occupy.
    """
    _, sim, client = served
    await _call(client, "pick_up_tips", {"tip_spots": ["labwire:deck/tips/A1"]})
    await _call(
        client, "set_well_volume", {"well": "labwire:deck/source_plate/A1", "volume_ul": 300.0}
    )
    sim.hold_types.add("aspirate")
    handle = await client.submit(
        "transfer",
        {
            "source": "labwire:deck/source_plate/A1",
            "targets": ["labwire:deck/target_plate/A1", "labwire:deck/target_plate/A2"],
            "volumes_ul": [40.0, 40.0],
        },
        confirmation=CONFIRMATION,
    )
    deadline = asyncio.get_event_loop().time() + 10.0
    while not sim.sent("aspirate"):
        assert asyncio.get_event_loop().time() < deadline
        await asyncio.sleep(0.01)
    status = await handle.cancel()
    assert status.status == "canceling"
    sim.release.set()
    while True:
        current = await handle.status()
        if current.status in ("succeeded", "failed", "canceled"):
            break
        assert asyncio.get_event_loop().time() < deadline
        await asyncio.sleep(0.01)

    assert current.status == "canceled"
    assert current.cancellation is not None
    assert current.cancellation.outcome == "halted_at_boundary"
    assert current.cancellation.boundary is not None
    assert current.cancellation.boundary.last == "aspirate"
    assert current.cancellation.boundary.completed_steps == 1
    assert current.cancellation.boundary.of_steps == 3

    assert sim.sent("dispense") == []  # the next step was never issued
    snapshot = await client.read_resource("labwire:deck")
    volumes = {well["uri"]: well["volume_ul"] for well in snapshot.content["contents"]}
    assert volumes["labwire:deck/source_plate/A1"] == 220.0  # 80 uL left in the tip
    assert "labwire:deck/target_plate/A1" not in volumes


# --- stop -------------------------------------------------------------------


async def test_stop_reaches_the_robot_and_says_so_honestly(
    served: tuple[Any, FlexServerSim, LabwireClient],
) -> None:
    """S0 stop posts the run stop action; its description disclaims settlement."""
    _, sim, client = served
    descriptor = await client.describe()
    stop_spec = next(spec for spec in descriptor.commands if spec.name == "stop")
    assert "does not mean motion has stopped" in stop_spec.description
    handle = await client.submit("stop", {})  # S0: no confirmation needed
    result = await handle.result(timeout=20.0)
    assert result["stopped"] is True
    assert sim.actions == [{"actionType": "stop"}]


# --- the pinned driver's single-channel-first shape is enforced -------------


async def test_multi_element_calls_are_refused_not_misrecorded(
    served: tuple[Any, FlexServerSim, LabwireClient],
) -> None:
    """The bridge refuses the arity rather than let the record diverge.

    The pinned driver executes element [0] but records every element.
    """
    _, sim, client = served
    before = len(sim.commands)
    with pytest.raises(LabwireError, match="exactly one element"):
        await _call(
            client,
            "pick_up_tips",
            {"tip_spots": ["labwire:deck/tips/A1", "labwire:deck/tips/B1"]},
        )
    with pytest.raises(LabwireError, match="exactly one element"):
        await _call(
            client,
            "aspirate",
            {
                "wells": ["labwire:deck/source_plate/A1", "labwire:deck/source_plate/B1"],
                "volumes_ul": [10.0, 20.0],
            },
        )
    assert len(sim.commands) == before  # nothing reached the robot
    snapshot = await client.read_resource("labwire:deck")
    labware = {item["uri"]: item for item in snapshot.content["labware"]}
    assert labware["labwire:deck/tips"]["tips_available"] == 96  # record untouched
