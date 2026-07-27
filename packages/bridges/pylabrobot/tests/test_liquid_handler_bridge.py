import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from labwire.bridges.pylabrobot import AnnotationError, AnnotationFile, resolve
from labwire.bridges.pylabrobot.annotations import CommandAnnotation, ResourceAnnotation
from labwire.bridges.pylabrobot.bridge import PyLabRobotInstrument, map_error
from labwire.core import InstrumentServer, LabwireClient, MemoryTransport
from labwire.core.errors import (
    ConfirmationRequiredError,
    HardwareFaultError,
    InterlockError,
    ValidationError,
)
from pylabrobot.liquid_handling import LiquidHandler

GRANT = "operator-standing-grant"


@pytest.fixture
async def served(
    rig: LiquidHandler, tmp_path: Path
) -> AsyncIterator[tuple[LiquidHandler, LabwireClient]]:
    """The rig served over the protocol, with an operator grant configured."""
    server = InstrumentServer(
        PyLabRobotInstrument(rig),
        confirmation_token=GRANT,
        grant_store=tmp_path / "grants",
    )
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        yield rig, client
    await server.aclose()


async def _call(client: LabwireClient, name: str, params: dict[str, Any]) -> Any:
    handle = await client.submit(name, params, confirmation=GRANT)
    return await handle.result(timeout=20.0)


# --- the descriptor ---------------------------------------------------------


async def test_the_instrument_describes_itself_with_units_and_safety(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    descriptor = await client.describe()
    commands = {c.name: c for c in descriptor.commands}
    assert commands["aspirate"].safety_class == "S2"
    assert commands["aspirate"].unit_annotations["volumes_ul"] == "uL"
    assert commands["stop"].safety_class == "S0"
    assert {c.name: c.unit for c in descriptor.channels} == {
        "tips_mounted": "1",
        "volume_aspirated_ul": "uL",
        "volume_dispensed_ul": "uL",
    }


async def test_address_parameters_carry_typed_references_and_no_pattern(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    """The F1 fix: the reference declaration replaced the invented pattern.

    A pattern is satisfiable by invention; resource_ref points at the deck
    index instead, and rides inside the schema that travels to agents.
    """
    _rig, client = served
    descriptor = await client.describe()
    schema = next(c for c in descriptor.commands if c.name == "aspirate").params_schema
    items = schema["properties"]["wells"]["items"]
    assert "pattern" not in items
    assert items["resource_ref"] == {"kind": "container", "enumerated_by": "labwire:deck"}
    assert "labwire:deck" in items["description"]  # the pointer an agent reads


async def test_the_descriptor_declares_the_deck_resource(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    descriptor = await client.describe()
    assert [r.uri for r in descriptor.resources] == ["labwire:deck"]
    assert "container" in descriptor.resources[0].item_kinds


async def test_optional_parameters_are_not_required(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    descriptor = await client.describe()
    schema = next(c for c in descriptor.commands if c.name == "aspirate").params_schema
    assert set(schema["required"]) == {"wells", "volumes_ul"}


# --- a real protocol end to end ---------------------------------------------


async def test_a_full_transfer_runs_through_the_protocol(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    """Tips on, aspirate, dispense, tips off, with the deck read at each step."""
    _rig, client = served
    await _call(
        client, "set_well_volume", {"well": "labwire:deck/source_plate/A1", "volume_ul": 300.0}
    )

    await _call(client, "pick_up_tips", {"tip_spots": ["labwire:deck/tips/A1"]})
    snapshot = await client.read_resource("labwire:deck")
    assert snapshot.content["channels"][0]["has_tip"] is True

    await _call(
        client, "aspirate", {"wells": ["labwire:deck/source_plate/A1"], "volumes_ul": [100.0]}
    )
    await _call(
        client, "dispense", {"wells": ["labwire:deck/target_plate/A1"], "volumes_ul": [100.0]}
    )
    await _call(client, "return_tips", {})

    snapshot = await client.read_resource("labwire:deck")
    volumes = {well["uri"]: well["volume_ul"] for well in snapshot.content["contents"]}
    assert volumes["labwire:deck/source_plate/A1"] == 200.0
    assert volumes["labwire:deck/target_plate/A1"] == 100.0
    assert snapshot.content["channels"][0]["has_tip"] is False


async def test_transfer_moves_liquid_into_several_wells(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    await _call(
        client, "set_well_volume", {"well": "labwire:deck/source_plate/A1", "volume_ul": 300.0}
    )
    await _call(client, "pick_up_tips", {"tip_spots": ["labwire:deck/tips/A1"]})
    result = await _call(
        client,
        "transfer",
        {
            "source": "labwire:deck/source_plate/A1",
            "targets": ["labwire:deck/target_plate/A1", "labwire:deck/target_plate/B1"],
            "volumes_ul": [50.0, 75.0],
        },
    )
    assert result["total_volume_ul"] == 125.0
    snapshot = await client.read_resource("labwire:deck")
    volumes = {well["uri"]: well["volume_ul"] for well in snapshot.content["contents"]}
    assert volumes["labwire:deck/target_plate/A1"] == 50.0
    assert volumes["labwire:deck/target_plate/B1"] == 75.0


async def test_eight_channels_aspirate_a_column_at_once(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    """JSON arrays carry cardinality, which is why the range DSL is not exposed."""
    _rig, client = served
    column = [f"labwire:deck/source_plate/{row}1" for row in "ABCDEFGH"]
    for well in column:
        await _call(client, "set_well_volume", {"well": well, "volume_ul": 200.0})
    await _call(
        client, "pick_up_tips", {"tip_spots": [f"labwire:deck/tips/{row}1" for row in "ABCDEFGH"]}
    )
    result = await _call(client, "aspirate", {"wells": column, "volumes_ul": [50.0] * 8})
    assert result["total_volume_ul"] == 400.0


async def test_telemetry_reports_cumulative_volume(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    async with client.telemetry(["volume_dispensed_ul"]) as subscription:
        await _call(
            client, "set_well_volume", {"well": "labwire:deck/source_plate/A1", "volume_ul": 300.0}
        )
        await _call(client, "pick_up_tips", {"tip_spots": ["labwire:deck/tips/A1"]})
        await _call(
            client, "aspirate", {"wells": ["labwire:deck/source_plate/A1"], "volumes_ul": [80.0]}
        )
        await _call(
            client, "dispense", {"wells": ["labwire:deck/target_plate/A1"], "volumes_ul": [80.0]}
        )
        async with asyncio.timeout(20.0):
            async for sample in subscription:
                if sample.value == 80.0:
                    break


# --- safety -----------------------------------------------------------------


async def test_moving_liquid_without_a_confirmation_is_refused(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    with pytest.raises(ConfirmationRequiredError):
        await client.submit(
            "aspirate", {"wells": ["labwire:deck/source_plate/A1"], "volumes_ul": [10.0]}
        )


async def test_reading_the_deck_needs_no_confirmation(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    """A resource read is not a command: no class, no confirmation, no run."""
    _rig, client = served
    snapshot = await client.read_resource("labwire:deck")
    assert snapshot.content["labware"]
    assert snapshot.index  # and it enumerates the reference targets


async def test_a_locked_plate_refuses_every_operation_touching_it(rig: LiquidHandler) -> None:
    """The one escalation v0.2 can enforce, since S2 and S3 gate identically."""
    annotations = AnnotationFile(
        resources={"labwire:deck/source_plate": ResourceAnnotation(locked=True)}
    )
    import tempfile

    server = InstrumentServer(
        PyLabRobotInstrument(rig, annotations),
        confirmation_token=GRANT,
        grant_store=Path(tempfile.mkdtemp(prefix="labwire-grants-")),
    )
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        handle = await client.submit(
            "aspirate",
            {"wells": ["labwire:deck/source_plate/A1"], "volumes_ul": [10.0]},
            confirmation=GRANT,
        )
        with pytest.raises(InterlockError, match="locked"):
            await handle.result(timeout=20.0)
        # an unlocked plate is unaffected
        await client.submit(
            "set_well_volume",
            {"well": "labwire:deck/target_plate/A1", "volume_ul": 10.0},
            confirmation=GRANT,
        )
    await server.aclose()


async def test_an_annotation_can_exclude_a_command_entirely(rig: LiquidHandler) -> None:
    annotations = AnnotationFile(commands={"transfer": CommandAnnotation(exclude=True)})
    instrument = PyLabRobotInstrument(rig, annotations)
    assert "transfer" not in {c.name for c in instrument.describe().commands}
    assert "aspirate" in {c.name for c in instrument.describe().commands}


async def test_an_annotation_can_raise_a_commands_safety_class(rig: LiquidHandler) -> None:
    annotations = AnnotationFile(commands={"dispense": CommandAnnotation(safety_class="S3")})
    commands = {c.name: c for c in PyLabRobotInstrument(rig, annotations).describe().commands}
    assert commands["dispense"].safety_class == "S3"


async def test_an_annotation_for_labware_that_is_not_loaded_is_refused(rig: LiquidHandler) -> None:
    """A silently ignored hazard annotation is the worst possible failure."""
    annotations = AnnotationFile(resources={"acid_stock": ResourceAnnotation(hazard="corrosive")})
    with pytest.raises(AnnotationError, match="acid_stock"):
        PyLabRobotInstrument(rig, annotations)


# --- errors -----------------------------------------------------------------


async def test_aspirating_with_no_tip_is_an_interlock_not_a_crash(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    await _call(
        client, "set_well_volume", {"well": "labwire:deck/source_plate/A1", "volume_ul": 300.0}
    )
    handle = await client.submit(
        "aspirate",
        {"wells": ["labwire:deck/source_plate/A1"], "volumes_ul": [10.0]},
        confirmation=GRANT,
    )
    with pytest.raises(InterlockError, match="tip"):
        await handle.result(timeout=20.0)


async def test_overdrawing_a_well_is_a_validation_error(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    await _call(
        client, "set_well_volume", {"well": "labwire:deck/source_plate/A1", "volume_ul": 50.0}
    )
    await _call(client, "pick_up_tips", {"tip_spots": ["labwire:deck/tips/A1"]})
    handle = await client.submit(
        "aspirate",
        {"wells": ["labwire:deck/source_plate/A1"], "volumes_ul": [500.0]},
        confirmation=GRANT,
    )
    with pytest.raises(ValidationError, match="Not enough liquid"):
        await handle.result(timeout=20.0)


async def test_an_unknown_well_is_refused_before_anything_moves(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    """The server's reference walk refuses it; no run is ever created."""
    from labwire.core import UnknownReferenceError

    _rig, client = served
    with pytest.raises(UnknownReferenceError) as caught:
        await client.submit(
            "aspirate",
            {"wells": ["labwire:deck/source_plate/Z99"], "volumes_ul": [10.0]},
            confirmation=GRANT,
        )
    details = caught.value.details
    assert details is not None
    assert details["reason"] == "no_such_item"
    assert details["resolved_prefix"] == "labwire:deck/source_plate"
    assert details["read"] == {"method": "resource/read", "params": {"uri": "labwire:deck"}}


async def test_a_malformed_reference_is_refused_as_unknown_reference(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    """No pattern exists to catch shape; the reference walk refuses instead."""
    from labwire.core import UnknownReferenceError

    _rig, client = served
    with pytest.raises(UnknownReferenceError) as caught:
        await client.submit(
            "aspirate", {"wells": ["not a valid address"], "volumes_ul": [10.0]}, confirmation=GRANT
        )
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "malformed_uri"


async def test_mismatched_addresses_and_volumes_are_refused(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    handle = await client.submit(
        "aspirate",
        {
            "wells": ["labwire:deck/source_plate/A1", "labwire:deck/source_plate/B1"],
            "volumes_ul": [10.0],
        },
        confirmation=GRANT,
    )
    with pytest.raises(ValidationError, match="one to one"):
        await handle.result(timeout=20.0)


async def test_declaring_more_volume_than_a_well_holds_is_refused(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    handle = await client.submit(
        "set_well_volume",
        {"well": "labwire:deck/source_plate/A1", "volume_ul": 10_000.0},
        confirmation=GRANT,
    )
    with pytest.raises(ValidationError, match="overfill"):
        await handle.result(timeout=20.0)


def test_unrecognized_exceptions_become_hardware_faults() -> None:
    """PyLabRobot has no common base exception, so the default must be safe."""
    assert isinstance(map_error(RuntimeError("board on fire")), HardwareFaultError)
    assert isinstance(map_error(ZeroDivisionError()), HardwareFaultError)


def test_a_channelized_error_keeps_every_channel_that_failed() -> None:
    """Collapsing it to one message would lose which channel went wrong."""
    from pylabrobot.liquid_handling.errors import ChannelizedError
    from pylabrobot.resources.errors import NoTipError

    mapped = map_error(ChannelizedError({0: NoTipError("no tip"), 3: NoTipError("no tip")}))
    assert isinstance(mapped, InterlockError)
    assert mapped.details is not None
    assert set(mapped.details["channels"]) == {"0", "3"}


# --- cancellation, honestly -------------------------------------------------


async def test_cancelling_a_finished_command_reports_it_cannot_be_cancelled(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    """The chatterbox backend completes instantly, so a cancel loses the race.

    This is the honest state of cancellation for this bridge: it is delivered,
    and against a simulated backend the command has almost always already won.
    """
    from labwire.core.errors import NotCancelableError

    _rig, client = served
    handle = await client.submit("stop", {})
    await handle.result(timeout=20.0)
    with pytest.raises(NotCancelableError):
        await handle.cancel()


async def test_stop_stays_available_and_reports_success(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    handle = await client.submit("stop", {})
    assert (await handle.result(timeout=20.0)) == {"stopped": True}


# --- signed evidence --------------------------------------------------------


async def test_a_run_produces_a_verifiable_signed_bundle(rig: LiquidHandler, tmp_path: Any) -> None:
    from labwire.core.signing import verify_bundle

    server = InstrumentServer(
        PyLabRobotInstrument(rig),
        confirmation_token=GRANT,
        manifest_dir=tmp_path,
        grant_store=tmp_path / "grants",
    )
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        resolve(rig, "labwire:deck/source_plate/A1").tracker.set_volume(300.0)
        await _call(client, "pick_up_tips", {"tip_spots": ["labwire:deck/tips/A1"]})
        handle = await client.submit(
            "aspirate",
            {"wells": ["labwire:deck/source_plate/A1"], "volumes_ul": [60.0]},
            confirmation=GRANT,
        )
        await handle.result(timeout=20.0)
        bundle = tmp_path / handle.command_id
    await server.aclose()

    result = verify_bundle(bundle)
    assert result.ok, result.errors
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["command"]["name"] == "aspirate"
    assert manifest["command"]["safety_class"] == "S2"  # recorded, not just enforced
    assert manifest["command"]["params"]["volumes_ul"] == [60.0]


# --- the gripper: S3, and the ceremony that makes it different --------------


async def test_the_standing_s2_grant_does_not_move_a_plate(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    """The confirmation that moved 800 uL of liquid does not move one plate."""
    from labwire.core import AuthorizationRequiredError

    _rig, client = served
    with pytest.raises(AuthorizationRequiredError) as caught:
        await client.submit(
            "move_plate",
            {"plate": "labwire:deck/target_plate", "to": "labwire:deck/staging-0"},
            confirmation=GRANT,
        )
    details = caught.value.details
    assert details is not None
    assert details["reason"] == "absent"
    assert details["mintable_by_agent"] is False
    assert details["request_id"].startswith("req-")


async def test_an_approved_grant_moves_the_plate_and_binds_to_it(
    rig: LiquidHandler, tmp_path: Path
) -> None:
    """Refusal, approval, success, and then the beat that proves the binding:
    the same valid grant refused on different parameters."""
    from datetime import timedelta

    from labwire.core import AuthorizationRequiredError

    server = InstrumentServer(
        PyLabRobotInstrument(rig), confirmation_token=GRANT, grant_store=tmp_path / "g"
    )
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        params = {"plate": "labwire:deck/target_plate", "to": "labwire:deck/staging-0"}
        with pytest.raises(AuthorizationRequiredError) as refused:
            await client.submit("move_plate", params)
        assert refused.value.details is not None
        request_id = refused.value.details["request_id"]

        store = server._grant_store  # pyright: ignore[reportPrivateUsage]
        assert store is not None
        grant = store.approve(
            request_id, now=server.clock.now(), ttl=timedelta(minutes=15), max_uses=2
        )

        handle = await client.submit("move_plate", params, authorization=grant.grant_id)
        moved = await handle.result(timeout=20.0)
        assert moved["to"] == "labwire:deck/staging-0"
        assert moved["origin"] == "labwire:deck/deck"

        # a valid, unexpired, correct-command grant on OTHER parameters
        with pytest.raises(AuthorizationRequiredError) as mismatched:
            await client.submit(
                "move_plate",
                {"plate": "labwire:deck/source_plate", "to": "labwire:deck/staging-1"},
                authorization=grant.grant_id,
            )
        assert mismatched.value.details is not None
        assert mismatched.value.details["reason"] == "params_mismatch"
    await server.aclose()


async def test_a_gripper_move_to_a_container_is_a_kind_mismatch(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    """A well is not a site, and the reference walk says so before authorization."""
    from labwire.core import UnknownReferenceError

    _rig, client = served
    with pytest.raises(UnknownReferenceError) as caught:
        await client.submit(
            "move_plate",
            {"plate": "labwire:deck/target_plate", "to": "labwire:deck/source_plate/A1"},
        )
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "kind_mismatch"


async def test_gripper_moves_are_not_interruptible(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    descriptor = await client.describe()
    movers = {c.name: c.interruptible for c in descriptor.commands}
    assert movers["move_plate"] is False
    assert movers["move_lid"] is False
    assert movers["move_resource"] is False


async def test_the_manifest_of_a_granted_run_records_the_ceremony(
    rig: LiquidHandler, tmp_path: Path
) -> None:
    from datetime import timedelta

    from labwire.core import AuthorizationRequiredError, verify_bundle

    server = InstrumentServer(
        PyLabRobotInstrument(rig),
        confirmation_token=GRANT,
        manifest_dir=tmp_path,
        grant_store=tmp_path / "grants",
    )
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        params = {"plate": "labwire:deck/target_plate", "to": "labwire:deck/staging-0"}
        with pytest.raises(AuthorizationRequiredError) as refused:
            await client.submit("move_plate", params)
        assert refused.value.details is not None
        store = server._grant_store  # pyright: ignore[reportPrivateUsage]
        assert store is not None
        grant = store.approve(
            refused.value.details["request_id"],
            now=server.clock.now(),
            ttl=timedelta(minutes=15),
            max_uses=1,
            issued_by="priya",
        )
        handle = await client.submit("move_plate", params, authorization=grant.grant_id)
        await handle.result(timeout=20.0)
        bundle = tmp_path / handle.command_id
    await server.aclose()

    assert verify_bundle(bundle).ok
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["command"]["safety_class"] == "S3"
    auth = manifest["authorization"]
    assert auth["mode"] == "grant"
    assert auth["identity_verified"] is False
    assert auth["use_index"] == 1
    assert auth["issued_by"] == "priya"
    assert "grant_id" not in json.dumps(manifest)  # a bearer value never lands in a bundle
    assert manifest["command"]["params_digest"].startswith("sha256:")


# --- discovery hygiene: the mechanical guards behind the no-hint claim ------
#
# The claim is that discovery ALONE leads an agent to the deck resource. These
# tests cannot prove model behaviour, and do not try; they prove the
# preconditions hold and cannot silently rot: nothing in the descriptor gives
# an agent material to invent a reference from, and the pointer to the deck
# rides inside every reference-taking parameter.


async def test_no_reference_parameter_declares_a_pattern(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    """A pattern is satisfiable by invention; none may ride with a reference."""
    _rig, client = served
    descriptor = await client.describe()
    for spec in descriptor.commands:
        for path, _ref in spec.references():
            assert "pattern" not in str(spec.params_schema), (spec.name, path)


async def test_every_reference_points_at_a_declared_resource(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    _rig, client = served
    descriptor = await client.describe()
    declared = {r.uri: set(r.item_kinds) for r in descriptor.resources}
    for spec in descriptor.commands:
        for path, ref in spec.references():
            assert ref["enumerated_by"] in declared, (spec.name, path)
            assert ref["kind"] in declared[ref["enumerated_by"]], (spec.name, path)


async def test_the_descriptor_contains_no_labware_names(
    served: tuple[LiquidHandler, LabwireClient],
) -> None:
    """What mechanically prevents reintroducing an address grammar.

    Labware names live in resource state, not in discovery, so the descriptor
    holds no material to assemble a reference value from. The only
    constructible prefix in an agent's context is labwire:deck, which is a
    read, not a submit.
    """
    rig, client = served
    descriptor = await client.describe()
    text = descriptor.model_dump_json()
    for name in {child.name for child in rig.deck.children}:
        if "_" in name or "-" in name:
            # A user-styled identifier appearing anywhere is a leak: it is the
            # exact spelling a URI needs.
            assert name not in text, f"descriptor leaks labware name {name!r}"
        else:
            # A common word (trash, tips, staging) may appear as English; what
            # must not appear is any path spelling of it.
            for spelled in (f"/{name}", f"{name}/"):
                assert spelled not in text, f"descriptor spells a path with {name!r}"


def test_the_agent_demo_prompt_contains_no_discovery_hints() -> None:
    """The specification must not live in the prompt (finding F2).

    The residual prompt may state the goal, the technique, and the safety
    facts. It may not tell the agent to read the deck first, name the deck
    resource, or spell any labware name the way a URI needs.
    """
    import re
    from pathlib import Path

    source = Path(__file__).parents[3].parents[0] / "examples" / "liquid_handling"
    text = (source / "claude_dilution.py").read_text()
    match = re.search(r'SYSTEM_PROMPT = f?"""(.*?)"""', text, re.DOTALL)
    assert match is not None
    prompt = match.group(1).lower()
    for hint in (
        "labwire:",
        "deck",
        "describe",
        "resource",
        "read",
        "source_plate",
        "dilution_plate",
        "target_plate",
        "tips/",
    ):
        assert hint not in prompt, f"prompt still hints: {hint!r}"
