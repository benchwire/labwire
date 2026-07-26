"""`make demo-claude`: a real Claude agent closes the loop over the instruments.

Claude discovers the instruments' commands as tools (the same mapping the
MCP adapter uses), plans its own experiments, and optimizes the reaction —
every action going through the Labwire protocol. Requires ANTHROPIC_API_KEY;
without it, this degrades gracefully to the scripted optimizer.

Run:
    uv run python examples/demo/claude_agent.py
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from labwire.core import CommandSpec, LabwireClient, LabwireError, verify_bundle
from rig import (
    DISPENSE_UL,
    RATE_RANGE,
    STANDING_GRANT,
    VOLT_RANGE,
    DemoRig,
    reactor_temp_c,
    yield_fraction,
)

MAX_ROUNDS = 40
_HIDDEN_COMMANDS = {"clear_protection", "clear_occlusion"}

SYSTEM_PROMPT = f"""You are operating a real (simulated) chemistry rig over the Labwire \
instrument-control protocol. Goal: maximize the yield of a flow reaction.

The rig: a power supply drives the reactor heater (higher voltage = hotter), a syringe \
pump feeds reagent (the flow rate sets residence time), and an analytical balance weighs \
the product collected after each dispense.

Procedure for ONE experiment:
1. psu_set_voltage (allowed {VOLT_RANGE[0]}-{VOLT_RANGE[1]} V; also ensure psu_output on once)
2. pump_dispense with volume_ul={DISPENSE_UL} and your chosen rate_ul_min \
(allowed {RATE_RANGE[0]}-{RATE_RANGE[1]}). This command is safety class S2 \
(irreversible: it consumes reagent), so it also needs \
confirmation="{STANDING_GRANT}" — the standing grant the operator issued for \
this session.
3. balance_measure to weigh the product (the harness collects it onto the balance)

Theoretical maximum product is {DISPENSE_UL * 0.005:.3f} g per experiment. Run at most \
12 experiments, adapting your search from the measurements. When confident, stop calling \
tools and reply with one line: FINAL: {{"volts": <v>, "rate_ul_min": <q>, "best_g": <m>}}"""


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


async def build_tools(
    rig: DemoRig,
) -> tuple[list[dict[str, Any]], dict[str, tuple[LabwireClient, CommandSpec]]]:
    """Instrument commands -> Anthropic tool definitions (MCP-style mapping)."""
    tools: list[dict[str, Any]] = []
    registry: dict[str, tuple[LabwireClient, CommandSpec]] = {}
    for label, client in [
        ("psu", rig.psu_client),
        ("pump", rig.pump_client),
        ("balance", rig.balance_client),
    ]:
        descriptor = await client.describe()
        for spec in descriptor.commands:
            if spec.name.startswith("x-") or spec.name in _HIDDEN_COMMANDS:
                continue
            name = f"{label}_{_sanitize(spec.name)}"
            registry[name] = (client, spec)
            schema = dict(spec.params_schema)
            note = f" Safety class {spec.safety_class}."
            if spec.safety_class in ("S2", "S3"):
                properties = dict(schema.get("properties", {}))
                properties["confirmation"] = {
                    "type": "string",
                    "description": "Operator confirmation string (required for S2/S3).",
                }
                schema["properties"] = properties
                note += " Requires a confirmation value."
            tools.append(
                {
                    "name": name,
                    "description": (
                        f"{spec.description.strip()}{note} [{descriptor.identity.model}]"
                    ),
                    "input_schema": schema,
                }
            )
    return tools, registry


async def execute_tool(
    rig: DemoRig,
    registry: dict[str, tuple[LabwireClient, CommandSpec]],
    name: str,
    arguments: dict[str, Any],
) -> str:
    """Run one tool call through the protocol; the harness reacts the chemistry."""
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
    if spec.name == "dispense":
        # chemistry happens between devices: product lands on the balance
        psu = await rig.psu_client.submit("measure", {}, confirmation=STANDING_GRANT)
        volts = float((await psu.result(timeout=30.0))["volts"])
        temp_c = reactor_temp_c(volts)
        product_g = (
            DISPENSE_UL * 0.005 * yield_fraction(temp_c, float(arguments.get("rate_ul_min", 0.0)))
        )
        for prep, params in [
            ("x-sim/load", {"mass_g": 0.0}),
            ("tare", {}),
            ("x-sim/load", {"mass_g": product_g}),
        ]:
            step = await rig.balance_client.submit(prep, params, confirmation=STANDING_GRANT)
            await step.result(timeout=30.0)
    return json.dumps(result)


async def run_claude(rig: DemoRig, api_key: str) -> None:
    """The agent loop: Claude plans, the rig executes, measurements guide it."""
    from anthropic import AsyncAnthropic

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    client = AsyncAnthropic(api_key=api_key)
    tools, registry = await build_tools(rig)
    print(f"claude agent online ({model}); tools: {[t['name'] for t in tools]}")
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "The rig is ready. Optimize the reaction yield."}
    ]
    for _ in range(MAX_ROUNDS):
        response = await client.messages.create(
            model=model,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=tools,  # pyright: ignore[reportArgumentType]
            messages=messages,  # pyright: ignore[reportArgumentType]
        )
        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"claude: {block.text.strip()}")
            elif block.type == "tool_use":
                arguments: dict[str, Any] = dict(block.input)
                print(f"  tool -> {block.name} {json.dumps(arguments)}")
                output = await execute_tool(rig, registry, block.name, arguments)
                print(f"  tool <- {output}")
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "user", "content": tool_results})


async def main() -> None:
    """Run the Claude-driven demo, or fall back to the scripted optimizer."""
    manifest_dir = Path(os.environ.get("DEMO_RUNS_DIR", "demo_runs"))
    fast = os.environ.get("DEMO_FAST") == "1"
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    async with await DemoRig.start(manifest_dir, time_scale=240.0 if fast else 60.0) as demo_rig:
        if api_key:
            await run_claude(demo_rig, api_key)
        else:
            print("ANTHROPIC_API_KEY not set - falling back to the scripted optimizer\n")
            from closed_loop import optimize

            results = await optimize(demo_rig, 4 if fast else 14)
            best = max(results, key=lambda r: r.product_g)
            print(
                f"\nbest yield {best.yield_pct:.1f}% at {best.volts:.1f} V, "
                f"{best.rate_ul_min:.0f} uL/min"
            )
        bundle = demo_rig.latest_bundle()
        assert bundle is not None, "no signed bundle was produced"
        outcome = verify_bundle(bundle)
        print(f"\nsigned evidence: {bundle}")
        print(f"  labwire verify: {'OK - authentic' if outcome.ok else outcome.errors}")
        if not outcome.ok:
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
