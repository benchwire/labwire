"""`make demo-ophyd`: scan a simulated beamline rig through Labwire.

Two ordinary ophyd devices: an `ophyd.sim` stage and detector, are exposed
by the bridge as Labwire instruments, with units and safety classes supplied
by the annotation file. The scan moves the stage across a range, acquires at
each point, finds the peak, and verifies the signed bundle for the winning
acquisition.

Run:
    uv run python examples/ophyd_scan/scan.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from labwire.core import verify_bundle
from rig import SCAN_RANGE, STANDING_GRANT, ScanRig


async def show_capabilities(rig: ScanRig) -> None:
    """Print what an agent discovers: units and safety classes, per command."""
    for label, client in (("stage", rig.stage_client), ("detector", rig.detector_client)):
        descriptor = await client.describe()
        identity = descriptor.identity
        print(
            f"{label}: {identity.model} ({identity.serial_number}) via {identity.firmware_version}"
        )
        for channel in descriptor.channels:
            print(f"    channel  {channel.name:24} {channel.unit}")
        for command in descriptor.commands:
            units = ", ".join(f"{k}={v}" for k, v in command.unit_annotations.items())
            print(f"    {command.safety_class}       {command.name:24} {units}")


async def main() -> None:
    """Scan the stage, find the detector peak, verify the signed evidence."""
    fast = os.environ.get("DEMO_FAST") == "1"
    points = 5 if fast else 17
    runs = Path(os.environ.get("DEMO_RUNS_DIR", "demo_runs_ophyd"))

    print("labwire ophyd bridge demo: peak-finding scan over a simulated beamline rig")
    print("devices: ophyd.sim.SynAxis (stage) + ophyd.sim.SynGauss (detector)\n")

    async with await ScanRig.start(runs) as rig:
        await show_capabilities(rig)
        print(
            f"\nsafety:  stage move is S2 (it displaces the sample); running under the "
            f"operator standing grant {STANDING_GRANT!r}"
        )
        print("         detector trigger is S1: acquisition needs no confirmation\n")

        low, high = SCAN_RANGE
        step = (high - low) / (points - 1)
        best = (float("-inf"), 0.0, "")
        streamed = 0

        async with rig.detector_client.telemetry(["point_detector"]) as counts_stream:

            async def count_samples() -> None:
                nonlocal streamed
                async for _sample in counts_stream:
                    streamed += 1

            watcher = asyncio.create_task(count_samples())
            for index in range(points):
                target = low + index * step
                landed = await rig.move_to(target)
                intensity, run_id = await rig.acquire()
                marker = "*" if intensity > best[0] else " "
                print(
                    f"  point {index + 1:02d}/{points}  stage={landed:+6.2f} mm   "
                    f"counts={intensity:8.1f}  {marker}"
                )
                if intensity > best[0]:
                    best = (intensity, landed, run_id)
            watcher.cancel()

        intensity, position, run_id = best
        print(
            f"\npeak found: {intensity:.1f} counts at {position:+.2f} mm "
            f"({streamed} telemetry samples streamed during the scan)"
        )

        bundle = rig.bundle_for(run_id)
        outcome = verify_bundle(bundle)
        print(f"\nsigned evidence for the peak acquisition: {bundle}")
        print(f"  labwire verify: {'OK - authentic' if outcome.ok else outcome.errors}")
        if not outcome.ok:
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
