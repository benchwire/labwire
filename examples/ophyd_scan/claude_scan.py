"""`make demo-ophyd-claude`: a Claude agent scans a simulated beamline rig.

The agent discovers two ordinary ophyd devices as tools, with the UCUM units
and safety classes the annotation file supplies, plans its own scan, and
must present the operator's confirmation for every S2 stage move. Requires
ANTHROPIC_API_KEY; without one it falls back to the scripted scan.

Run:
    uv run python examples/ophyd_scan/claude_scan.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from labwire.core import CommandSpec, LabwireClient, LabwireError, verify_bundle
from rig import SCAN_RANGE, STANDING_GRANT, ScanRig

MAX_ROUNDS = 40

SYSTEM_PROMPT = f"""You are operating a beamline rig over the Labwire protocol. The \
hardware is driven by ophyd, the same layer used at synchrotron beamlines, exposed to \
you as tools.

Goal: find the stage position where the detector response peaks, within \
{SCAN_RANGE[0]} to {SCAN_RANGE[1]} mm.

Method: move the stage, then trigger the detector to acquire a reading at that \
position. Repeat, narrowing in on the peak. Budget: at most 12 acquisitions.

Safety: every tool tells you its safety class. Moving the stage is class S2 because it \
displaces the sample, so those calls require confirmation="{STANDING_GRANT}": the \
standing grant this session's operator issued. Acquisition is S1 and needs no \
confirmation. Never invent a confirmation value for anything else.

Units are UCUM codes given per parameter; positions are in mm and detector counts are \
dimensionless counts. When you are confident, stop calling tools and reply with one \
line: FINAL: {{"position_mm": <p>, "counts": <c>}}"""


async def build_tools(
    rig: ScanRig,
) -> tuple[list[dict[str, Any]], dict[str, tuple[LabwireClient, CommandSpec]]]:
    """Labwire descriptors -> agent tools, carrying units and safety classes."""
    tools: list[dict[str, Any]] = []
    registry: dict[str, tuple[LabwireClient, CommandSpec]] = {}
    for label, client in (("stage", rig.stage_client), ("detector", rig.detector_client)):
        descriptor = await client.describe()
        for spec in descriptor.commands:
            if spec.name == "stop":
                continue  # the agent has no reason to e-stop during a scan
            name = f"{label}_{spec.name}"
            registry[name] = (client, spec)
            schema = dict(spec.params_schema)
            notes = [spec.description.strip(), f"Safety class {spec.safety_class}."]
            if spec.unit_annotations:
                units = ", ".join(f"{k} in {v}" for k, v in spec.unit_annotations.items())
                notes.append(f"Units (UCUM): {units}.")
            if spec.safety_class in ("S2", "S3"):
                properties = dict(schema.get("properties", {}))
                properties["confirmation"] = {
                    "type": "string",
                    "description": "Operator confirmation string, required for this class.",
                }
                schema["properties"] = properties
                notes.append("Requires a confirmation value.")
            tools.append(
                {
                    "name": name,
                    "description": " ".join(notes) + f" [{descriptor.identity.model}]",
                    "input_schema": schema,
                }
            )
    return tools, registry


async def execute_tool(
    registry: dict[str, tuple[LabwireClient, CommandSpec]],
    name: str,
    arguments: dict[str, Any],
    acquisitions: list[str],
) -> str:
    """Run one tool call through the Labwire protocol.

    Acquisition run ids are recorded as they happen, so the demo can verify
    the exact bundle the agent produced rather than guessing at the newest
    file on disk.
    """
    if name not in registry:
        return f"ERROR: no such tool {name!r}"
    client, spec = registry[name]
    payload = dict(arguments)
    confirmation = payload.pop("confirmation", None)
    try:
        handle = await client.submit(
            spec.name,
            payload,
            confirmation=str(confirmation) if confirmation is not None else None,
        )
        result = await handle.result(timeout=120.0)
    except (LabwireError, TimeoutError) as exc:
        return f"ERROR: {exc}"
    if spec.name == "trigger":
        acquisitions.append(handle.command_id)
    return json.dumps(result)


async def run_claude(rig: ScanRig, api_key: str, acquisitions: list[str]) -> None:
    """The agent loop: Claude plans the scan, the rig executes it."""
    from anthropic import AsyncAnthropic

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    client = AsyncAnthropic(api_key=api_key)
    tools, registry = await build_tools(rig)
    print(f"claude agent online ({model})")
    for tool in tools:
        print(f"  tool: {tool['name']}")
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "The rig is ready. Find the detector peak."}
    ]
    for _ in range(MAX_ROUNDS):
        response = await client.messages.create(
            model=model,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=tools,  # pyright: ignore[reportArgumentType]
            messages=messages,  # pyright: ignore[reportArgumentType]
        )
        results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"claude: {block.text.strip()}")
            elif block.type == "tool_use":
                arguments: dict[str, Any] = dict(block.input)
                print(f"  tool -> {block.name} {json.dumps(arguments)}")
                output = await execute_tool(registry, block.name, arguments, acquisitions)
                print(f"  tool <- {output}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "user", "content": results})


async def main() -> None:
    """Run the agent-driven scan, or the scripted one without a key."""
    runs = Path(os.environ.get("DEMO_RUNS_DIR", "demo_runs_ophyd"))
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    acquisitions: list[str] = []
    async with await ScanRig.start(runs) as rig:
        if api_key:
            await run_claude(rig, api_key, acquisitions)
            run_id = acquisitions[-1] if acquisitions else ""
        else:
            print("ANTHROPIC_API_KEY not set - falling back to the scripted scan\n")
            best = (float("-inf"), 0.0, "")
            low, high = SCAN_RANGE
            for index in range(5):
                target = low + index * (high - low) / 4
                landed = await rig.move_to(target)
                counts, run_id = await rig.acquire()
                print(f"  stage={landed:+6.2f} mm  counts={counts:8.1f}")
                if counts > best[0]:
                    best = (counts, landed, run_id)
            print(f"\npeak: {best[0]:.1f} counts at {best[1]:+.2f} mm")
            run_id = best[2]

        if not run_id:
            print("no acquisition was made, so there is no signed evidence")
            sys.exit(1)
        bundle = rig.bundle_for(run_id)
        outcome = verify_bundle(bundle)
        print(f"\nsigned evidence: {bundle}")
        print(f"  labwire verify: {'OK - authentic' if outcome.ok else outcome.errors}")
        if not outcome.ok:
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
