"""`make demo-pylabrobot-claude`: a Claude agent runs a dilution series.

The agent reads the deck over the protocol, plans its own transfers, and must
present the operator's confirmation for every S2 command that moves liquid.
Requires ANTHROPIC_API_KEY; without one it falls back to the scripted
dilution.

This is the part of the exercise that is actually hard for the protocol. The
agent cannot plan anything until it knows what is on the deck, and Labwire
v0.2 has nowhere to publish that, so the deck arrives as the result of a
command the agent has to remember to call. Its first tool call is always
describe_deck, and it is prompted to make it. See SPEC-FINDINGS.md.

Run:
    uv run python examples/liquid_handling/claude_dilution.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from labwire.core import CommandSpec, LabwireClient, LabwireError, verify_bundle
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

MAX_ROUNDS = 60

SYSTEM_PROMPT = f"""You are operating a liquid handler over the Labwire protocol. The \
machine is driven by PyLabRobot, exposed to you as tools.

Goal: run a two-fold serial dilution across row A of the dilution plate, \
{{steps}} steps of {STEP_VOLUME_UL:.0f} uL each, starting from the dye stock in well A1 \
of the source plate and carrying forward one well at a time.

Technique: use a fresh tip for each step. Pick up one tip, transfer, then discard it \
before the next step. Carrying one tip through the series would contaminate it.

When the series is complete, move the dilution plate to an empty staging site with \
move_plate.

Safety: every tool tells you its safety class. Liquid handling is S2 and requires \
confirmation="{STANDING_GRANT}", the standing grant this session's operator issued. \
S3 tools take an operator grant in the authorization field instead; a confirmation \
never satisfies them, and you can never invent either value. If an S3 call is refused, \
report the refusal and wait; if your operator gives you a grant id, present it for \
exactly the same call.

Units are UCUM codes given per parameter; volumes are in uL. If a call fails, the \
error names what would have worked.

When the series is done and the plate is moved, stop calling tools and reply with one \
line: FINAL: {{{{"steps_completed": <n>}}}}"""


async def build_tools(
    rig: DilutionRig,
) -> tuple[list[dict[str, Any]], dict[str, CommandSpec]]:
    """Labwire descriptors to agent tools, carrying units and safety classes."""
    tools: list[dict[str, Any]] = []
    registry: dict[str, CommandSpec] = {}
    descriptor = await rig.client.describe()
    if descriptor.resources:
        # The read is model-callable, not host-optional: the uri parameter is
        # an enum of the declared resources, so the model cannot get it wrong.
        tools.append(
            {
                "name": "read_resource",
                "description": (
                    "Read one of this instrument's resources: its typed content and "
                    "the index of everything a command parameter can reference. "
                    + " ".join(f"{r.uri}: {r.description}" for r in descriptor.resources)
                ),
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["uri"],
                    "properties": {
                        "uri": {"type": "string", "enum": [r.uri for r in descriptor.resources]}
                    },
                },
            }
        )
    for spec in descriptor.commands:
        if spec.name == "stop":
            continue  # the agent has no reason to halt the machine mid-series
        registry[spec.name] = spec
        schema = dict(spec.params_schema)
        notes = [spec.description.strip(), f"Safety class {spec.safety_class}."]
        if spec.unit_annotations:
            units = ", ".join(f"{k} in {v}" for k, v in spec.unit_annotations.items())
            notes.append(f"Units (UCUM): {units}.")
        if spec.safety_class == "S2":
            properties = dict(schema.get("properties", {}))
            properties["confirmation"] = {
                "type": "string",
                "description": "Operator confirmation string, required for this class.",
            }
            schema["properties"] = properties
            notes.append("Requires a confirmation value.")
        elif spec.safety_class == "S3":
            properties = dict(schema.get("properties", {}))
            properties["authorization"] = {
                "type": "object",
                "additionalProperties": False,
                "required": ["grant_id"],
                "description": (
                    "Operator grant. You cannot mint this. Present only an id an "
                    "operator gave you for this exact call."
                ),
                "properties": {"grant_id": {"type": "string"}},
            }
            schema["properties"] = properties
            notes.append(
                "HAZARDOUS: requires an operator grant bound to these exact parameter "
                "values; a confirmation string will not authorize it. If you hold no "
                "grant, call once WITHOUT authorization and report the refusal."
            )
        tools.append(
            {
                "name": spec.name,
                "description": " ".join(notes) + f" [{descriptor.identity.model}]",
                "input_schema": schema,
            }
        )
    return tools, registry


async def execute_tool(
    client: LabwireClient,
    registry: dict[str, CommandSpec],
    name: str,
    arguments: dict[str, Any],
    transfers: list[str],
) -> str:
    """Run one tool call through the Labwire protocol.

    Transfer run ids are recorded as they happen, so the demo verifies the
    exact bundle the agent produced rather than the newest file on disk.
    """
    if name == "read_resource":
        snapshot = await client.read_resource(str(arguments["uri"]))
        return snapshot.model_dump_json(exclude_none=True)
    if name not in registry:
        return f"ERROR: no such tool {name!r}"
    spec = registry[name]
    payload = dict(arguments)
    confirmation = payload.pop("confirmation", None)
    authorization = payload.pop("authorization", None)
    grant_id = authorization.get("grant_id") if isinstance(authorization, dict) else None
    try:
        handle = await client.submit(
            spec.name,
            payload,
            confirmation=str(confirmation) if confirmation is not None else None,
            authorization=str(grant_id) if grant_id is not None else None,
        )
        result = await handle.result(timeout=120.0)
    except (LabwireError, TimeoutError) as exc:
        details = getattr(exc, "details", None)
        if details:
            # Flattening would destroy request_id, did_you_mean, and the
            # ready-to-send read; the recovery path lives in these fields.
            return json.dumps(
                {"error": str(exc), "category": getattr(exc, "category", None), "details": details}
            )
        return f"ERROR: {exc}"
    if spec.name in {"transfer", "dispense"}:
        transfers.append(handle.command_id)
    return json.dumps(result)


def _pending_request_id(results: list[dict[str, Any]]) -> str | None:
    """The request id of an authorization_required refusal, if one just happened."""
    for entry in results:
        content = entry.get("content")
        if not isinstance(content, str) or "authorization_required" not in content:
            continue
        try:
            details = json.loads(content).get("details", {})
        except ValueError:
            continue
        if details.get("reason") == "absent" and details.get("request_id"):
            return str(details["request_id"])
    return None


def _operator_approves(rig: DilutionRig, request_id: str) -> str:
    """The operator role: approve one pending request from the server's store.

    NOTE: demo and operator run as one user on one machine here; nothing in
    this process enforces the separation. On a real bench the store lives
    where the agent cannot write it.
    """
    from datetime import UTC, datetime, timedelta

    from labwire.core import GrantStore

    store = GrantStore(rig.grant_dir, serial_number="lh_deck")
    grant = store.approve(
        request_id,
        now=datetime.now(UTC),
        ttl=timedelta(minutes=15),
        max_uses=1,
        issued_by="operator",
    )
    print(f"  [operator] approved {request_id} -> grant {grant.grant_id[:10]}... (1 use)")
    return grant.grant_id


async def prepare(rig: DilutionRig, steps: int) -> None:
    """Declare what the plates hold, as a human loading the deck would."""
    await rig.call(
        "set_well_volume", {"well": "labwire:deck/source_plate/A1", "volume_ul": DYE_VOLUME_UL}
    )
    for well in dilution_wells(steps):
        await rig.call("set_well_volume", {"well": well, "volume_ul": DILUENT_VOLUME_UL})
    print(
        f"operator loaded the deck: {DYE_VOLUME_UL:.0f} uL dye in the source plate, "
        f"{DILUENT_VOLUME_UL:.0f} uL diluent in {steps} dilution wells\n"
    )


async def run_claude(rig: DilutionRig, api_key: str, steps: int, transfers: list[str]) -> None:
    """The agent loop: Claude plans the dilution, the machine executes it."""
    from anthropic import AsyncAnthropic

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    anthropic = AsyncAnthropic(api_key=api_key)
    tools, registry = await build_tools(rig)
    print(f"claude agent online ({model})")
    for tool in tools:
        print(f"  tool: {tool['name']}")
    print()
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "The deck is loaded. Run the dilution series."}
    ]
    operator_acted = False
    for _ in range(MAX_ROUNDS):
        response = await anthropic.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_PROMPT.replace("{steps}", str(steps)),
            tools=tools,  # pyright: ignore[reportArgumentType]
            messages=messages,  # pyright: ignore[reportArgumentType]
        )
        results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"claude: {block.text.strip()}")
            elif block.type == "tool_use":
                arguments: dict[str, Any] = dict(block.input)
                print(f"  tool -> {block.name} {json.dumps(arguments)[:160]}")
                output = await execute_tool(rig.client, registry, block.name, arguments, transfers)
                print(f"  tool <- {output[:220]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            # If the agent stopped because an S3 call was refused, the OPERATOR
            # (this harness, playing that role on the same machine) approves
            # the pending request and hands the agent a single-use grant id.
            request_id = _pending_request_id(results)
            if request_id and not operator_acted:
                operator_acted = True
                grant_id = _operator_approves(rig, request_id)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Operator here. I reviewed request {request_id} on the "
                            f"instrument host and approved it: grant id {grant_id}, "
                            "single use, expires in 15 minutes. Proceed with exactly "
                            "the call you reported."
                        ),
                    }
                )
                continue
            break
        messages.append({"role": "user", "content": results})


async def run_scripted(rig: DilutionRig, steps: int, transfers: list[str]) -> None:
    """The same series, planned by this script rather than by an agent."""
    source = "labwire:deck/source_plate/A1"
    for index, target in enumerate(dilution_wells(steps)):
        await rig.call("pick_up_tips", {"tip_spots": [f"labwire:deck/tips/A{index + 1}"]})
        _result, run_id = await rig.call(
            "transfer", {"source": source, "targets": [target], "volumes_ul": [STEP_VOLUME_UL]}
        )
        transfers.append(run_id)
        await rig.call("discard_tips")
        print(f"  step {index + 1}: {source:18} -> {target:18} 1:{2 ** (index + 1)}")
        source = target


async def main() -> None:
    """Run the agent-driven dilution, or the scripted one without a key."""
    runs = Path(os.environ.get("DEMO_RUNS_DIR", "demo_runs_pylabrobot"))
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    steps = demo_steps()
    transfers: list[str] = []

    async with await DilutionRig.start(runs) as rig:
        await prepare(rig, steps)
        if api_key:
            await run_claude(rig, api_key, steps, transfers)
        else:
            print("ANTHROPIC_API_KEY not set - falling back to the scripted dilution\n")
            await run_scripted(rig, steps, transfers)
            transfers.append(await gripper_act(rig))

        snapshot = await rig.client.read_resource("labwire:deck")
        print("\nfinal deck contents:")
        for well in snapshot.content["contents"]:
            print(f"    {well['uri']:36} {well['volume_ul']:7.1f} uL")

        if not transfers:
            print("\nno liquid was moved, so there is no signed evidence")
            sys.exit(1)
        bundle = rig.bundle_for(transfers[-1])
        outcome = verify_bundle(bundle)
        print(f"\nsigned evidence: {bundle}")
        print(f"  labwire verify: {'OK - authentic' if outcome.ok else outcome.errors}")
        if not outcome.ok:
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
