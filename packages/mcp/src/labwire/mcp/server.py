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
import secrets
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, cast

from labwire.core import CommandSpec, InstrumentDescriptor, LabwireClient, LabwireError

from mcp import MCPError
from mcp.server import CacheHint, Server, ServerRequestContext
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequiredResult,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
    ToolAnnotations,
)

_DEFAULT_TIMEOUT_S = 300.0
_LEGACY_ERA_PREFIX = "202" + "5"  # protocol versions 2025-* and earlier use the handshake era
_MODERN_ERA = "2026-07-28"


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
            destructive_hint=True,
            idempotent_hint=False,
        )
    if spec.safety_class == "S2":
        return ToolAnnotations(title="Irreversible: confirmation required", destructive_hint=True)
    return None


@dataclass
class _PendingApproval:
    """One in-flight input_required round, held in-process (SPEC 8.6 note).

    The request_state travelling to the client is only a random key into
    this table; nothing decodable leaves the process, and the stdio
    adapter IS one process, so no sealing crypto is needed. Single-use.
    """

    tool: str
    arguments: dict[str, Any]
    kind: str  # "s2" or "s3"
    request_id: str | None = None  # the S3 pending request, for the docs trail


def _era_is_modern(ctx: ServerRequestContext[Any, Any]) -> bool:
    return not ctx.protocol_version.startswith(_LEGACY_ERA_PREFIX)


def build_server(
    instruments: list[ConnectedInstrument],
    *,
    s2_confirmation: str | None = None,
) -> Server[Any]:
    """Build an MCP server exposing every instrument command as a tool.

    ``s2_confirmation`` is the deployment's standing S2 confirmation,
    read from the LABWIRE_MCP_CONFIRMATION environment variable by the
    console entry point (an environment variable, never a CLI flag, so
    it cannot leak through process listings). When set and the client
    speaks 2026-07-28, S2 calls become a client-surfaced approval: the
    human sees the exact command and parameters and approves or
    declines, and the adapter injects the confirmation only on approval.
    Neither path identifies WHO approved; that honesty caveat is the
    same one the signed manifests carry (identity_verified false).

    Example:
        >>> # server = build_server(instruments)
    """
    pending: dict[str, _PendingApproval] = {}

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        del ctx, params
        tools = [
            Tool(
                name=tool_name,
                description=_tool_description(instrument, spec),
                input_schema=_tool_input_schema(spec),
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
                    input_schema={
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
                    annotations=ToolAnnotations(read_only_hint=True),
                )
            )
        return ListToolsResult(tools=tools)

    async def list_resources(
        ctx: ServerRequestContext[Any, Any], params: PaginatedRequestParams | None
    ) -> ListResourcesResult:
        del ctx, params
        # The namespaced form exists only where MCP requires a globally unique
        # uri; reference values everywhere the model acts keep the wire
        # spelling, so there is no bidirectional rewriting to get wrong.
        return ListResourcesResult(
            resources=[
                Resource(
                    uri=f"labwire://{instrument.prefix}/{spec.uri.removeprefix('labwire:')}",
                    name=f"{instrument.prefix}: {spec.title}",
                    description=spec.description,
                    mime_type="application/json",
                )
                for instrument in instruments
                for spec in instrument.descriptor.resources
            ]
        )

    async def read_resource(
        ctx: ServerRequestContext[Any, Any], params: ReadResourceRequestParams
    ) -> ReadResourceResult:
        del ctx
        text = str(params.uri)
        for instrument in instruments:
            marker = f"labwire://{instrument.prefix}/"
            if text.startswith(marker):
                wire_uri = "labwire:" + text.removeprefix(marker)
                snapshot = await instrument.client.read_resource(wire_uri)
                return ReadResourceResult(
                    contents=[
                        TextResourceContents(
                            uri=text,
                            mime_type="application/json",
                            text=snapshot.model_dump_json(exclude_none=True),
                        )
                    ]
                )
        raise MCPError(code=INVALID_PARAMS, message=f"unknown resource: {params.uri}")

    async def call_tool(
        ctx: ServerRequestContext[Any, Any], params: CallToolRequestParams
    ) -> CallToolResult | InputRequiredResult:
        name = params.name
        arguments = dict(params.arguments or {})
        for instrument in instruments:
            if name == f"{instrument.prefix}__read_resource":
                snapshot = await instrument.client.read_resource(str(arguments["uri"]))
                return _ok(snapshot.model_dump_json(exclude_none=True))
            spec = instrument.commands.get(name)
            if spec is not None:
                return await _run_command(
                    ctx, instrument, spec, name, params, pending, s2_confirmation
                )
        raise MCPError(code=INVALID_PARAMS, message=f"unknown tool: {name}")

    return Server(
        name="labwire",
        # Tool and resource surfaces are fixed for the life of the process
        # (they derive from the configured instruments), so lists cache
        # generously; a resource READ is live instrument state (the deck
        # changes with every liquid move) and must not be cached at all.
        cache_hints={
            "tools/list": CacheHint(ttl_ms=300_000, scope="private"),
            "resources/list": CacheHint(ttl_ms=300_000, scope="private"),
            "resources/read": CacheHint(ttl_ms=0, scope="private"),
        },
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_read_resource=read_resource,
    )


def _ok(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


def _tool_error(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))], is_error=True
    )


def _approval_request(
    instrument: ConnectedInstrument, spec: CommandSpec, arguments: dict[str, Any]
) -> ElicitRequest:
    """The S2 approval the client surfaces to the human (form mode, yes/no)."""
    shown = {k: v for k, v in arguments.items() if k not in ("confirmation", "authorization")}
    message = (
        f"Approve running {spec.name!r} on "
        f"{instrument.descriptor.identity.model}? Safety class S2: costly or "
        f"irreversible. Exact parameters: {json.dumps(shown, sort_keys=True)}. "
        "Approving supplies this deployment's standing confirmation; it does "
        "not identify who approved."
    )
    return ElicitRequest(
        method="elicitation/create",
        params=ElicitRequestFormParams(
            message=message,
            requested_schema={
                "type": "object",
                "properties": {
                    "approve": {
                        "type": "boolean",
                        "description": "true to run the command, false to refuse",
                    }
                },
                "required": ["approve"],
            },
        ),
    )


def _grant_request(
    instrument: ConnectedInstrument, spec: CommandSpec, refusal: dict[str, Any]
) -> ElicitRequest:
    """The S3 elicitation: the operator mints a grant and the human types it.

    A grant id is minted per request by `labwire grant approve`; the
    adapter cannot pre-hold one, so typing it is not ceremony, it is the
    only possible flow.
    """
    details = cast("dict[str, Any]", refusal.get("details") or {})
    request_id = details.get("request_id", "?")
    instruction = details.get("operator_instruction") or (
        f"On the instrument host run: labwire grant list, then "
        f"labwire grant approve {request_id} --ttl 15m --uses 1"
    )
    message = (
        f"{spec.name!r} on {instrument.descriptor.identity.model} is safety "
        f"class S3 (hazardous) and needs an operator grant for exactly these "
        f"parameters. Pending request: {request_id}. {instruction} Then enter "
        "the grant id it printed."
    )
    return ElicitRequest(
        method="elicitation/create",
        params=ElicitRequestFormParams(
            message=message,
            requested_schema={
                "type": "object",
                "properties": {
                    "grant_id": {
                        "type": "string",
                        "description": "the grant id the operator command printed",
                    }
                },
                "required": ["grant_id"],
            },
        ),
    )


async def _run_command(
    ctx: ServerRequestContext[Any, Any],
    instrument: ConnectedInstrument,
    spec: CommandSpec,
    tool_name: str,
    params: CallToolRequestParams,
    pending: dict[str, _PendingApproval],
    s2_confirmation: str | None,
) -> CallToolResult | InputRequiredResult:
    arguments = dict(params.arguments or {})

    # --- resume an input_required round (modern era only) -------------------
    if params.request_state:
        entry = pending.pop(str(params.request_state), None)
        if entry is None or entry.tool != tool_name:
            return _tool_error(
                {
                    "error": "unknown or already-used request_state; start the call over",
                    "category": "validation",
                }
            )
        responses = {
            key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
            for key, value in (params.input_responses or {}).items()
        }
        answer = cast("dict[str, Any]", responses.get("approval") or {})
        action = answer.get("action")
        content = cast("dict[str, Any]", answer.get("content") or {})
        if entry.kind == "s2":
            if action != "accept" or content.get("approve") is not True:
                return _tool_error(
                    {
                        "error": f"operator declined {spec.name!r}; nothing was submitted",
                        "category": "confirmation_required",
                    }
                )
            arguments = dict(entry.arguments)
            arguments["confirmation"] = s2_confirmation
        else:  # s3
            grant_id = content.get("grant_id")
            if action != "accept" or not grant_id:
                return _tool_error(
                    {
                        "error": f"no grant supplied for {spec.name!r}; nothing was submitted",
                        "category": "authorization_required",
                    }
                )
            arguments = dict(entry.arguments)
            arguments["authorization"] = {"grant_id": str(grant_id)}

    # --- S2: modern era surfaces the approval to the human ------------------
    elif (
        spec.safety_class == "S2"
        and "confirmation" not in arguments
        and s2_confirmation is not None
        and _era_is_modern(ctx)
    ):
        token = secrets.token_urlsafe(24)
        pending[token] = _PendingApproval(tool=tool_name, arguments=arguments, kind="s2")
        return InputRequiredResult(
            input_requests={"approval": _approval_request(instrument, spec, arguments)},
            request_state=token,
        )

    outcome = await _submit(instrument, spec, arguments)

    # --- S3: turn the refusal into a client-surfaced grant entry ------------
    if (
        isinstance(outcome, dict)
        and outcome.get("category") == "authorization_required"
        and _era_is_modern(ctx)
        and not params.request_state
    ):
        token = secrets.token_urlsafe(24)
        details = cast("dict[str, Any]", outcome.get("details") or {})
        pending[token] = _PendingApproval(
            tool=tool_name,
            arguments=arguments,
            kind="s3",
            request_id=str(details.get("request_id", "")),
        )
        return InputRequiredResult(
            input_requests={"approval": _grant_request(instrument, spec, outcome)},
            request_state=token,
        )

    if isinstance(outcome, dict):
        return _tool_error(outcome)
    return _ok(outcome)


async def _submit(
    instrument: ConnectedInstrument, spec: CommandSpec, arguments: dict[str, Any]
) -> str | dict[str, Any]:
    """Run one command; a str is success JSON, a dict is a structured error."""
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
        # Flattening to str(exc) would destroy request_id, did_you_mean,
        # and the ready-to-send read request; the recovery paths the
        # protocol designed live in these fields, so the model gets them.
        return {
            "error": str(exc),
            "category": exc.category,
            "retryable": exc.retryable,
            "details": exc.details,
        }
    except TimeoutError:
        return {
            "error": f"command {spec.name!r} did not reach a terminal state within {timeout:.0f} s",
            "category": "device_timeout",
            "retryable": False,
            "details": None,
        }
    return json.dumps(result)
