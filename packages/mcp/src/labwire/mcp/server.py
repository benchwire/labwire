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
from pydantic import AnyUrl

from mcp.server.lowlevel import Server
from mcp.types import Resource, TextContent, Tool, ToolAnnotations

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
        "This tool does NOT accept a confirmation string and a session "
        "confirmation will not authorize it. It requires an operator grant, "
        "provisioned outside this protocol and bound to this command and these "
        "exact parameter values, expiring and use-limited. You cannot create, "
        "request, or derive one. If you do not hold a grant for these exact "
        "values, call this tool once WITHOUT authorization: the server will "
        "refuse it and return a request id and the exact command a human "
        "operator must run. Report that to your operator and stop. Never "
        "invent a grant id."
    ),
}


_CANCEL_NOTES: dict[str, str] = {
    # SPEC 8.3: agents plan around irreversibility, so each tool says what
    # cancel can physically do BEFORE the agent commits to calling it.
    "none": (
        "Cancel: none. Once started this runs to completion; the operation "
        "is committed to the device and a cancel request will be refused. "
        "Decide before calling, not after."
    ),
    "between_steps": (
        "Cancel: between steps only. A cancel finishes the step in flight "
        "and stops at the next boundary; the record names the boundary "
        "reached, and partial physical effects (such as liquid already "
        "aspirated) remain."
    ),
    "abort": (
        "Cancel: abort. The backend has a real halt path; a cancelled run's "
        "record states whether the halt was confirmed, and 'unconfirmed' "
        "means the physical state must be treated as unknown."
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
    parts.append(_CANCEL_NOTES[spec.cancel_semantics])
    parts.append(
        f"Instrument: {identity.manufacturer} {identity.model} "
        f"(SN {identity.serial_number}); Labwire command {spec.name!r}."
    )
    return " ".join(part for part in parts if part)


def _tool_input_schema(spec: CommandSpec) -> dict[str, Any]:
    """The command's params schema, plus its authorization field (SPEC §8.6).

    S2 adds a required ``confirmation``. S3 adds ``authorization``, and
    deliberately **not** required: the discovery-by-refusal first call must be
    a well-formed tool call a host will not block, and the two fields are
    never both present, because a confirmation cannot satisfy S3.
    """
    if spec.safety_class == "S2":
        schema = deepcopy(spec.params_schema)
        properties = schema.setdefault("properties", {})
        if isinstance(properties, dict):
            cast("dict[str, Any]", properties)["confirmation"] = {
                "type": "string",
                "description": (
                    "Operator confirmation required for this S2 command. "
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
    if spec.safety_class == "S3":
        schema = deepcopy(spec.params_schema)
        properties = schema.setdefault("properties", {})
        if isinstance(properties, dict):
            cast("dict[str, Any]", properties)["authorization"] = {
                "type": "object",
                "additionalProperties": False,
                "required": ["grant_id"],
                "description": (
                    "Operator grant. You cannot mint this. Present only an id an "
                    "operator gave you for this exact call."
                ),
                "properties": {"grant_id": {"type": "string"}},
            }
        return schema
    return spec.params_schema


def _tool_annotations(spec: CommandSpec) -> ToolAnnotations | None:
    """MCP hints for the upper classes; unset elsewhere rather than asserted.

    ``readOnlyHint`` stays unset even for S0/S1 because Labwire cannot yet
    tell a read from a state edit (finding F6, out of scope), and unset says
    unknown rather than asserting something false.
    """
    if spec.safety_class == "S3":
        return ToolAnnotations(
            title="HAZARDOUS: operator grant required",
            destructiveHint=True,
            idempotentHint=False,
        )
    if spec.safety_class == "S2":
        return ToolAnnotations(title="Irreversible: confirmation required", destructiveHint=True)
    return None


def build_server(instruments: list[ConnectedInstrument]) -> Server:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    """Build an MCP server exposing every instrument command as a tool.

    Example:
        >>> # server = build_server(instruments)
    """
    server = Server("labwire")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:  # pyright: ignore[reportUnusedFunction]
        tools = [
            Tool(
                name=tool_name,
                description=_tool_description(instrument, spec),
                inputSchema=_tool_input_schema(spec),
                annotations=_tool_annotations(spec),
            )
            for instrument in instruments
            for tool_name, spec in instrument.commands.items()
        ]
        for instrument in instruments:
            if not instrument.descriptor.resources:
                continue
            # The read is model-callable, not host-optional: MCP resources are
            # application-controlled and many hosts surface them only through
            # a human-driven picker, so a discovery story cannot rest on them
            # alone. The uri parameter is an enum, so the model cannot get it
            # wrong.
            tools.append(
                Tool(
                    name=f"{instrument.prefix}__read_resource",
                    description=(
                        "Read one of this instrument's resources: its typed content "
                        "and the index of everything a command parameter can "
                        "reference. "
                        + " ".join(
                            f"{r.uri}: {r.description}" for r in instrument.descriptor.resources
                        )
                    ),
                    inputSchema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["uri"],
                        "properties": {
                            "uri": {
                                "type": "string",
                                "enum": [r.uri for r in instrument.descriptor.resources],
                            }
                        },
                    },
                    annotations=ToolAnnotations(readOnlyHint=True),
                )
            )
        return tools

    @server.list_resources()
    async def _list_resources() -> list[Resource]:  # pyright: ignore[reportUnusedFunction]
        # The namespaced form exists only where MCP requires a globally unique
        # uri; reference values everywhere the model acts keep the wire
        # spelling, so there is no bidirectional rewriting to get wrong.
        return [
            Resource(
                uri=AnyUrl(f"labwire://{instrument.prefix}/{spec.uri.removeprefix('labwire:')}"),
                name=f"{instrument.prefix}: {spec.title}",
                description=spec.description,
                mimeType="application/json",
            )
            for instrument in instruments
            for spec in instrument.descriptor.resources
        ]

    @server.read_resource()
    async def _read_resource(uri: AnyUrl) -> str:  # pyright: ignore[reportUnusedFunction]
        text = str(uri)
        for instrument in instruments:
            marker = f"labwire://{instrument.prefix}/"
            if text.startswith(marker):
                wire_uri = "labwire:" + text.removeprefix(marker)
                snapshot = await instrument.client.read_resource(wire_uri)
                return snapshot.model_dump_json(exclude_none=True)
        raise ValueError(f"unknown resource: {uri}")

    @server.call_tool()
    async def _call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        for instrument in instruments:
            if name == f"{instrument.prefix}__read_resource":
                snapshot = await instrument.client.read_resource(str(arguments["uri"]))
                return [TextContent(type="text", text=snapshot.model_dump_json(exclude_none=True))]
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
    authorization = cast("dict[str, Any] | None", payload.pop("authorization", None))
    grant_id = authorization.get("grant_id") if isinstance(authorization, dict) else None
    try:
        handle = await instrument.client.submit(
            spec.name,
            payload,
            confirmation=str(confirmation) if confirmation is not None else None,
            authorization=str(grant_id) if grant_id is not None else None,
        )
        result = await handle.result(timeout=timeout)
    except LabwireError as exc:
        if exc.details:
            # Flattening to str(exc) would destroy request_id, did_you_mean,
            # and the ready-to-send read request; the recovery paths the
            # protocol designed live in these fields, so the model gets them.
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": str(exc),
                            "category": exc.category,
                            "retryable": exc.retryable,
                            "details": exc.details,
                        }
                    ),
                )
            ]
        raise ValueError(
            f"{exc.category} error from {instrument.descriptor.identity.model}: {exc} "
            f"(retryable: {exc.retryable})"
        ) from exc
    except TimeoutError as exc:
        raise ValueError(
            f"command {spec.name!r} did not reach a terminal state within {timeout:.0f} s"
        ) from exc
    return [TextContent(type="text", text=json.dumps(result))]
