"""`make demo-pylabrobot`: a serial dilution driven through Labwire.

An ordinary PyLabRobot liquid handler is exposed by the bridge as a Labwire
instrument. This script reads the deck over the protocol, declares what the
plates hold, then runs a two-fold serial dilution across a row: fresh tip,
transfer, discard, repeat. At the end it reads the deck back and verifies the
signed bundle for one of the transfers.

The volumes are real, in the sense that PyLabRobot's own volume tracker
computes them and would have refused a physically impossible step. The
concentrations are arithmetic done by this script from the transfer ratios;
no chemistry is simulated.

Run:
    uv run python examples/liquid_handling/dilution.py
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from labwire.core import verify_bundle
from rig import (
    DILUENT_VOLUME_UL,
    DYE_VOLUME_UL,
    STANDING_GRANT,
    STEP_VOLUME_UL,
    DilutionRig,
    demo_steps,
    dilution_wells,
    gripper_act,
)


async def show_capabilities(rig: DilutionRig) -> None:
    """Print what an agent discovers: units and safety classes, per command."""
    descriptor = await rig.client.describe()
    identity = descriptor.identity
    print(
        f"instrument: {identity.model} ({identity.serial_number}) via {identity.firmware_version}"
    )
    for channel in descriptor.channels:
        print(f"    channel  {channel.name:22} {channel.unit}")
    for command in descriptor.commands:
        units = ", ".join(f"{k}={v}" for k, v in command.unit_annotations.items())
        print(f"    {command.safety_class}       {command.name:22} {units}")


async def show_deck(rig: DilutionRig, heading: str) -> dict[str, Any]:
    """Read the labwire:deck resource and print what is on it."""
    snapshot = await rig.client.read_resource("labwire:deck")
    state: dict[str, Any] = snapshot.content
    print(f"\n{heading}  (revision {snapshot.revision})")
    for item in state["labware"]:
        if item["kind"] not in {"plate", "tip_rack"}:
            continue
        extra = f"{item['tips_available']} tips left" if item["kind"] == "tip_rack" else ""
        grid = item.get("grid") or {}
        shape = f"{grid.get('rows')}x{grid.get('columns')}" if grid else ""
        print(f"    {item['uri']:36} {item['kind']:9} {shape:6} {extra}")
    return state


async def cancel_a_transfer_at_a_boundary(rig: "DilutionRig") -> None:
    """Cancel a transfer mid-flight and let the record tell the truth.

    transfer is the one command this bridge sequences itself, so it
    declares cancel_semantics "between_steps" (SPEC 8.3): the in-flight
    PLR call finishes, the next is never issued, and the settlement names
    the boundary. The aspirated liquid is in the tip, and the deck
    resource says so; nothing pretends the cancel undid anything.
    """
    import asyncio as _asyncio

    print("\ncancelling a transfer mid-flight (S2, cancel between steps)")
    await rig.call("set_well_volume", {"well": "labwire:deck/source_plate/B1", "volume_ul": 200.0})
    await rig.call("pick_up_tips", {"tip_spots": ["labwire:deck/tips/H12"]})
    handle = await rig.client.submit(
        "transfer",
        {
            "source": "labwire:deck/source_plate/B1",
            "targets": ["labwire:deck/dilution_plate/B1", "labwire:deck/dilution_plate/B2"],
            "volumes_ul": [40.0, 40.0],
        },
        confirmation=STANDING_GRANT,
    )
    await _asyncio.sleep(0.05)  # let the aspirate step start
    accepted = await handle.cancel()
    print(f"  cancel accepted: {accepted.status} (acknowledgment is not settlement)")
    while True:
        status = await handle.status()
        if status.status in ("succeeded", "failed", "canceled"):
            break
        await _asyncio.sleep(0.02)
    block = status.cancellation
    assert block is not None
    if block.boundary is not None:
        print(
            f"  settled: {status.status}, {block.outcome} after "
            f"{block.boundary.last!r} ({block.boundary.completed_steps}/"
            f"{block.boundary.of_steps} steps)"
        )
        print("  the aspirated liquid is in the tip: the deck shows the source's deficit")
    else:
        print(f"  settled: {status.status}, {block.outcome} (the steps outran the cancel)")
    await rig.call("discard_tips", {})


async def main() -> None:
    """Run the dilution series and verify the signed evidence."""
    steps = demo_steps()
    os.environ.setdefault("DEMO_OP_DELAY", "0.1")  # honest pacing; see rig._paced
    runs = Path(os.environ.get("DEMO_RUNS_DIR", "demo_runs_pylabrobot"))

    print("labwire pylabrobot bridge demo: two-fold serial dilution")
    print("machine: PyLabRobot LiquidHandler on a Hamilton STARlet deck (chatterbox backend)\n")

    async with await DilutionRig.start(runs) as rig:
        await show_capabilities(rig)
        print(
            f"\nsafety:  every liquid-moving command is S2 (irreversible); running under "
            f"the operator standing grant {STANDING_GRANT!r}"
        )

        await show_deck(rig, "deck as loaded:")

        # PyLabRobot cannot see into a plate a human placed on the deck, so the
        # run starts by telling it what is there. This moves nothing (S1).
        wells = dilution_wells(steps)
        await rig.call(
            "set_well_volume", {"well": "labwire:deck/source_plate/A1", "volume_ul": DYE_VOLUME_UL}
        )
        for well in wells:
            await rig.call("set_well_volume", {"well": well, "volume_ul": DILUENT_VOLUME_UL})
        print(
            f"\ndeclared: {DYE_VOLUME_UL:.0f} uL dye in source_plate/A1, "
            f"{DILUENT_VOLUME_UL:.0f} uL diluent in each of {len(wells)} dilution wells"
        )

        print("\nserial dilution, fresh tip per step:")
        source = "labwire:deck/source_plate/A1"
        transfer_runs: list[str] = []
        for index, target in enumerate(wells):
            tip = f"labwire:deck/tips/A{index + 1}"
            await rig.call("pick_up_tips", {"tip_spots": [tip]})
            _result, run_id = await rig.call(
                "transfer",
                {"source": source, "targets": [target], "volumes_ul": [STEP_VOLUME_UL]},
            )
            transfer_runs.append(run_id)
            await rig.call("discard_tips")
            nominal = 0.5 ** (index + 1)
            print(
                f"    step {index + 1}: {STEP_VOLUME_UL:.0f} uL {source:18} -> {target:18} "
                f"nominal 1:{2 ** (index + 1)} ({nominal:.4f})"
            )
            source = target

        state = await show_deck(rig, "deck after the run:")
        print("\n    contents:")
        for well in state["contents"]:
            print(f"        {well['uri']:36} {well['volume_ul']:7.1f} uL")

        mounted = sum(1 for channel in state["channels"] if channel["has_tip"])
        print(f"\n    channels holding a tip: {mounted} (every tip was discarded)")

        await cancel_a_transfer_at_a_boundary(rig)

        move_run = await gripper_act(rig)

        bundle = rig.bundle_for(move_run)
        print(f"\nsigned evidence: {bundle}")
        result = verify_bundle(bundle)
        status = "OK - authentic" if result.ok else f"FAILED: {'; '.join(result.errors)}"
        print(f"  labwire verify: {status}")
        import json as _json

        manifest = _json.loads((bundle / "manifest.json").read_text())
        auth = manifest.get("authorization", {})
        print(
            f"  command        {manifest['command']['name']}   "
            f"safety_class {manifest['command']['safety_class']}"
        )
        print(
            f"  authorization  mode={auth.get('mode')}  use {auth.get('use_index')}/1  "
            f'issued_by "{auth.get("issued_by")}" [unauthenticated note]'
        )
        print(
            f"  identity_verified {auth.get('identity_verified')}   "
            "<- deployment policy and parameter binding proven; NOT who"
        )
        if not result.ok:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
