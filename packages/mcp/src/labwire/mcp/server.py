"""The Labwire MCP adapter: instrument commands become MCP tools.

Connects to one or more Labwire Instrument Servers over WebSocket and
exposes every declared command as an MCP tool. The command's
``params_schema`` (already JSON Schema, SPEC §7.2) becomes the tool's
``inputSchema`` verbatim; tool names are ``<model>__<command>`` with
characters outside the MCP tool-name set replaced by ``_``.

Example:
    >>> # instruments = await connect_instruments(["ws://127.0.0.1:9520"])
    >>> # server = build_server(instruments)
"""

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, cast

from labwire.core import CommandSpec, InstrumentDescriptor, LabwireClient, LabwireError
from labwire.core.capabilities import CONFIRMATION_REQUIRED_CLASSES

from mcp.server.lowlevel import Server
from mcp.types import TextContent, Tool

_DEFAULT_TIMEOUT_S = 300.0


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


@dataclass
class ConnectedInstrument:
    """One live instrument connection and its tool namespace."""

    client: LabwireClient
    descriptor: InstrumentDescriptor
    prefix: str
    commands: dict[str, CommandSpec] = field(default_factory=dict[str, "CommandSpec"])


async def connect_instruments(urls: list[str]) -> list[ConnectedInstrument]:
    """Connect to each Instrument Server and build its tool namespace.

    Example:
        >>> # instruments = await connect_instruments(["ws://127.0.0.1:9520"])
    """
    instruments: list[ConnectedInstrument] = []
    taken_prefixes: set[str] = set()
    for url in urls:
        client = await LabwireClient.connect(url, client_name="labwire-mcp")
        await client.__aenter__()
        descriptor = await client.describe()
        base = _sanitize(descriptor.identity.model)
        prefix = base
        counter = 2
        while prefix in taken_prefixes:
            prefix = f"{base}_{counter}"
            counter += 1
        taken_prefixes.add(prefix)
        connected = ConnectedInstrument(client=client, descriptor=descriptor, prefix=prefix)
        for spec in descriptor.commands:
            connected.commands[f"{prefix}__{_sanitize(spec.name)}"] = spec
        instruments.append(connected)
    return instruments


_SAFETY_NOTES = {
    "S0": "Safety class S0 (emergency/protective operation; always permitted).",
    "S1": "Safety class S1 (routine, reversible).",
    "S2": (
        "Safety class S2 (costly or IRREVERSIBLE, e.g. consumes reagent or "
        "destroys a sample). Requires a `confirmation` value; supply the "
        "operator-provided confirmation string."
    ),
    "S3": (
        "Safety class S3 (HAZARDOUS, capable of harming people or equipment). "
        "Requires a `confirmation` value; do not invent one."
    ),
}


def _tool_description(instrument: ConnectedInstrument, spec: CommandSpec) -> str:
    identity = instrument.descriptor.identity
    parts = [spec.description.strip()]
    if spec.unit_annotations:
        units = ", ".join(f"{k} in {v}" for k, v in spec.unit_annotations.items())
        parts.append(f"Parameter units (UCUM): {units}.")
    if spec.returns_units:
        results = ", ".join(f"{k} in {v}" for k, v in spec.returns_units.items())
        parts.append(f"Result units (UCUM): {results}.")
    parts.append(_SAFETY_NOTES.get(spec.safety_class, ""))
    parts.append(
        f"Instrument: {identity.manufacturer} {identity.model} "
        f"(SN {identity.serial_number}); Labwire command {spec.name!r}."
    )
    return " ".join(part for part in parts if part)


def _tool_input_schema(spec: CommandSpec) -> dict[str, Any]:
    """The command's params schema, plus ``confirmation`` for S2/S3 (SPEC §8.6)."""
    if spec.safety_class not in CONFIRMATION_REQUIRED_CLASSES:
        return spec.params_schema
    schema = deepcopy(spec.params_schema)
    properties = schema.setdefault("properties", {})
    if isinstance(properties, dict):
        cast("dict[str, Any]", properties)["confirmation"] = {
            "type": "string",
            "description": (
                f"Operator confirmation required for this {spec.safety_class} command. "
                "Use the confirmation string the operator supplied for this session."
            ),
        }
    required = schema.get("required")
    schema["required"] = (
        [*cast("list[Any]", required), "confirmation"]
        if isinstance(required, list)
        else ["confirmation"]
    )
    return schema


def build_server(instruments: list[ConnectedInstrument]) -> Server:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    """Build an MCP server exposing every instrument command as a tool.

    Example:
        >>> # server = build_server(instruments)
    """
    server = Server("labwire")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:  # pyright: ignore[reportUnusedFunction]
        return [
            Tool(
                name=tool_name,
                description=_tool_description(instrument, spec),
                inputSchema=_tool_input_schema(spec),
            )
            for instrument in instruments
            for tool_name, spec in instrument.commands.items()
        ]

    @server.call_tool()
    async def _call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        for instrument in instruments:
            spec = instrument.commands.get(name)
            if spec is not None:
                return await _run_command(instrument, spec, arguments)
        raise ValueError(f"unknown tool: {name}")

    return server


async def _run_command(
    instrument: ConnectedInstrument, spec: CommandSpec, arguments: dict[str, Any]
) -> list[TextContent]:
    timeout = (
        spec.estimated_duration_s * 5.0
        if spec.estimated_duration_s is not None
        else _DEFAULT_TIMEOUT_S
    )
    payload = dict(arguments)
    confirmation = payload.pop("confirmation", None)
    try:
        handle = await instrument.client.submit(
            spec.name,
            payload,
            confirmation=str(confirmation) if confirmation is not None else None,
        )
        result = await handle.result(timeout=timeout)
    except LabwireError as exc:
        raise ValueError(
            f"{exc.category} error from {instrument.descriptor.identity.model}: {exc} "
            f"(retryable: {exc.retryable})"
        ) from exc
    except TimeoutError as exc:
        raise ValueError(
            f"command {spec.name!r} did not reach a terminal state within {timeout:.0f} s"
        ) from exc
    return [TextContent(type="text", text=json.dumps(result))]
