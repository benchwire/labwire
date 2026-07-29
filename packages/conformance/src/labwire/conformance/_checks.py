"""The executable checks, each tied to a normative spec requirement.

A check coroutine returns a detail string on PASS (usually empty) and raises
:class:`CheckFailed` with the observed behavior on FAIL, or
:class:`NotApplicable` / :class:`Unexercised` to skip. No check ever causes
a handler to execute: everything that submits commands stops at a refusal
the server must issue before running one, except the explicitly opted-in
exercise check.
"""

import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
from labwire.conformance._raw import RawWire
from labwire.conformance._synth import CannotSynthesize, minimal_params
from labwire.core import LabwireClient
from labwire.core.capabilities import CommandSpec, InstrumentDescriptor
from labwire.core.errors import LabwireError
from labwire.core.messages import ResourceReadResult


class CheckFailed(Exception):
    """The server did not do what the spec requires."""


class NotApplicable(Exception):
    """The server does not declare the capability this check tests."""


class Unexercised(Exception):
    """The operator did not opt in to what this check needs."""


@dataclass
class RunOptions:
    """Operator-supplied inputs for the checks that need them."""

    exercise: str | None = None
    exercise_params: dict[str, Any] = field(default_factory=dict)
    confirmation: str | None = None
    authorization: str | None = None
    bundle_dir: Path | None = None


@dataclass
class CheckContext:
    """Everything a check may draw on, prepared once by the runner."""

    url: str
    client: LabwireClient
    raw_descriptor: dict[str, Any]
    descriptor: InstrumentDescriptor | None
    resource_reads: dict[str, ResourceReadResult]
    options: RunOptions
    exercised_command_id: str | None = None

    def parsed(self) -> InstrumentDescriptor:
        """The descriptor, or NotApplicable when it failed to parse."""
        if self.descriptor is None:
            raise NotApplicable("descriptor did not validate; see core.describe.descriptor_valid")
        return self.descriptor

    def command_named(self, name: str) -> CommandSpec:
        """A declared command by name."""
        for spec in self.parsed().commands:
            if spec.name == name:
                return spec
        raise CheckFailed(f"command {name!r} is not declared by this instrument")

    def first_command(self, predicate: Callable[[CommandSpec], bool], why: str) -> CommandSpec:
        """The first declared command matching, or NotApplicable."""
        for spec in self.parsed().commands:
            if predicate(spec):
                return spec
        raise NotApplicable(why)

    def resolve_reference(self, annotation: dict[str, Any]) -> str:
        """A real item URI for a resource_ref, drawn from the live index.

        Entries can be referenced directly by their kinds; their items are
        the entry URI plus ``/<id>`` per the SPEC 10.1 composition rule.
        """
        wanted = annotation.get("kind")
        enumerated_by = annotation.get("enumerated_by")
        candidates = (
            [self.resource_reads[enumerated_by]]
            if enumerated_by in self.resource_reads
            else list(self.resource_reads.values())
        )
        for snapshot in candidates:
            for entry in snapshot.index:
                if wanted is None or wanted in entry.kinds:
                    return entry.uri
                if entry.children and wanted in entry.children.kinds and entry.children.ids:
                    return f"{entry.uri}/{entry.children.ids[0]}"
        raise CannotSynthesize(f"no live index entry of kind {wanted!r} to reference")

    def synthesize(self, spec: CommandSpec) -> dict[str, Any]:
        """Schema-valid params for a command, never to be executed."""
        try:
            return minimal_params(spec.params_schema, self.resolve_reference)
        except CannotSynthesize as exc:
            raise NotApplicable(f"cannot synthesize valid params for {spec.name!r}: {exc}") from exc


async def _expect_refusal(
    ctx: CheckContext,
    spec: CommandSpec,
    *,
    code: int,
    category: str,
    confirmation: str | None = None,
    params: dict[str, Any] | None = None,
) -> LabwireError:
    """Submit and demand a specific pre-execution refusal."""
    payload = ctx.synthesize(spec) if params is None else params
    try:
        await ctx.client.submit(spec.name, payload, confirmation=confirmation)
    except LabwireError as exc:
        if exc.code != code or exc.category != category:
            raise CheckFailed(
                f"{spec.name!r} was refused with code {exc.code} category "
                f"{exc.category!r}; the spec requires {code} {category!r}"
            ) from exc
        return exc
    raise CheckFailed(f"{spec.name!r} was accepted; the spec requires refusal {code} {category!r}")


# --- core ------------------------------------------------------------------


async def check_initialize_negotiates(ctx: CheckContext) -> str:
    """SPEC 4/6.1: the handshake succeeds and the server speaks 0.4."""
    async with RawWire(ctx.url) as wire:
        response = await wire.initialize()
        result = response.get("result")
        if not isinstance(result, dict):
            raise CheckFailed(f"initialize returned {response!r}, not a result object")
        version = result.get("protocol_version")
        if version != "0.4":
            raise CheckFailed(f"server negotiated protocol_version {version!r}, expected '0.4'")
        for key in ("server_info", "capabilities"):
            if key not in result:
                raise CheckFailed(f"initialize result is missing {key!r}")
    return ""


async def check_initialize_required(ctx: CheckContext) -> str:
    """SPEC 6.2: requests before initialize are refused with -32002."""
    async with RawWire(ctx.url) as wire:
        response = await wire.call("instrument/describe", {}, request_id=7)
        error = response.get("error")
        if not isinstance(error, dict) or error.get("code") != -32002:
            raise CheckFailed(f"describe before initialize got {response!r}, expected -32002")
    return ""


async def check_parse_error_recovery(ctx: CheckContext) -> str:
    """SPEC 12: malformed JSON gets -32700 and the session survives it."""
    async with RawWire(ctx.url) as wire:
        await wire.initialize()
        await wire.send_text("this is not json {")
        response = await wire.recv_json()
        error = response.get("error") if isinstance(response, dict) else None
        if not isinstance(error, dict) or error.get("code") != -32700:
            raise CheckFailed(f"malformed JSON got {response!r}, expected error -32700")
        alive = await wire.call("ping", {}, request_id=9)
        if "result" not in alive:
            raise CheckFailed("session did not survive a parse error; ping failed after it")
    return ""


async def check_method_not_found(ctx: CheckContext) -> str:
    """SPEC 12: an unknown method is refused with -32601."""
    async with RawWire(ctx.url) as wire:
        await wire.initialize()
        response = await wire.call("conformance/no-such-method", {}, request_id=11)
        error = response.get("error")
        if not isinstance(error, dict) or error.get("code") != -32601:
            raise CheckFailed(f"unknown method got {response!r}, expected -32601")
    return ""


async def check_ping(ctx: CheckContext) -> str:
    """SPEC 6.4: ping answers."""
    await ctx.client.ping()
    return ""


async def check_descriptor_valid(ctx: CheckContext) -> str:
    """SPEC 7: the descriptor validates against the message reference."""
    try:
        InstrumentDescriptor.model_validate(ctx.raw_descriptor)
    except Exception as exc:
        raise CheckFailed(f"descriptor does not validate: {exc}") from exc
    return ""


async def check_units_mandatory(ctx: CheckContext) -> str:
    """SPEC 7.2/7.3: every declared quantity carries a UCUM code.

    Validating each command declaration separately pinpoints the offender;
    the walk itself is the same fail-closed one the reference SDK enforces.
    """
    problems: list[str] = []
    for raw_command in ctx.raw_descriptor.get("commands", []):
        try:
            CommandSpec.model_validate(raw_command)
        except Exception as exc:
            problems.append(f"{raw_command.get('name', '?')}: {exc}")
    for raw_channel in ctx.raw_descriptor.get("channels", []):
        if not str(raw_channel.get("unit", "")).strip():
            problems.append(f"channel {raw_channel.get('name', '?')}: empty unit")
    if problems:
        raise CheckFailed("; ".join(problems[:3]) + (" ..." if len(problems) > 3 else ""))
    return ""


async def check_unsupported_command(ctx: CheckContext) -> str:
    """SPEC 8.2/12: submitting an undeclared command is refused as unsupported."""
    try:
        await ctx.client.submit("conformance-no-such-command", {})
    except LabwireError as exc:
        if exc.category != "unsupported":
            raise CheckFailed(
                f"undeclared command refused as {exc.category!r} (code {exc.code}); "
                "the spec requires category 'unsupported'"
            ) from exc
        return ""
    raise CheckFailed("an undeclared command was accepted")


async def check_validation_refusal(ctx: CheckContext) -> str:
    """SPEC 8.2/12: schema-invalid params are refused as validation."""
    spec = ctx.first_command(
        lambda s: bool(s.params_schema.get("required")),
        "no declared command has required parameters",
    )
    try:
        await ctx.client.submit(spec.name, {})
    except LabwireError as exc:
        if exc.category != "validation":
            raise CheckFailed(
                f"{spec.name!r} with missing required params refused as {exc.category!r}; "
                "the spec requires category 'validation'"
            ) from exc
        return ""
    raise CheckFailed(f"{spec.name!r} accepted params missing required fields")


async def check_lifecycle_exercise(ctx: CheckContext) -> str:
    """SPEC 8: one real command runs to a terminal status with a result.

    Opt-in: executing a command on someone's instrument is not something a
    conformance tool does uninvited. Pass --exercise on a safe deployment.
    """
    if ctx.options.exercise is None:
        raise Unexercised("pass --exercise COMMAND to run one real command on a safe deployment")
    spec = ctx.command_named(ctx.options.exercise)
    handle = await ctx.client.submit(
        spec.name,
        ctx.options.exercise_params,
        confirmation=ctx.options.confirmation,
        authorization=ctx.options.authorization,
    )
    await handle.result(timeout=60.0)
    status = await handle.status()
    if status.status != "succeeded":
        raise CheckFailed(f"exercised command ended {status.status!r}, expected 'succeeded'")
    ctx.exercised_command_id = handle.command_id
    return f"executed {spec.name!r}"


# --- safety ----------------------------------------------------------------


async def check_s2_refused_unconfirmed(ctx: CheckContext) -> str:
    """SPEC 8.6: an S2 submission without confirmation is refused -32009."""
    spec = ctx.first_command(lambda s: s.safety_class == "S2", "no S2 command is declared")
    exc = await _expect_refusal(ctx, spec, code=-32009, category="confirmation_required")
    detail = (exc.details or {}).get("safety_class")
    if detail not in (None, "S2"):
        raise CheckFailed(f"refusal details claim safety_class {detail!r} for an S2 command")
    return ""


async def check_s3_refused_ungranted(ctx: CheckContext) -> str:
    """SPEC 8.6: an S3 submission without a grant is refused -32011.

    A confirmation string must not satisfy it either, so one is sent.
    """
    spec = ctx.first_command(lambda s: s.safety_class == "S3", "no S3 command is declared")
    exc = await _expect_refusal(
        ctx,
        spec,
        code=-32011,
        category="authorization_required",
        confirmation="conformance-standing-confirmation",
    )
    mintable = (exc.details or {}).get("mintable_by_agent")
    if mintable is True:
        raise CheckFailed("refusal details claim the agent could mint the authorization")
    return ""


# --- resources -------------------------------------------------------------


def _declared_resources(ctx: CheckContext) -> list[Any]:
    resources = ctx.parsed().resources
    if not resources:
        raise NotApplicable("no resources are declared")
    return list(resources)


async def check_resources_read(ctx: CheckContext) -> str:
    """SPEC 10.2: every declared resource reads back shaped and typed."""
    problems: list[str] = []
    for declared in _declared_resources(ctx):
        snapshot = ctx.resource_reads.get(declared.uri)
        if snapshot is None:
            problems.append(f"{declared.uri}: read failed")
            continue
        if snapshot.uri != declared.uri:
            problems.append(f"{declared.uri}: read answered for {snapshot.uri!r}")
        if not snapshot.revision.strip():
            problems.append(f"{declared.uri}: empty revision")
        for entry in snapshot.index:
            if entry.uri != declared.uri and not entry.uri.startswith(declared.uri + "/"):
                problems.append(
                    f"{declared.uri}: index entry {entry.uri!r} does not compose "
                    "from the resource URI (SPEC 10.1)"
                )
                break
        if declared.content_schema and snapshot.content is not None:
            try:
                jsonschema.validate(snapshot.content, declared.content_schema)
            except jsonschema.ValidationError as exc:
                problems.append(f"{declared.uri}: content violates content_schema: {exc.message}")
    if problems:
        raise CheckFailed("; ".join(problems[:3]) + (" ..." if len(problems) > 3 else ""))
    return f"read {len(ctx.resource_reads)} resource(s)"


async def check_unknown_resource_refused(ctx: CheckContext) -> str:
    """SPEC 10.2: reading an undeclared resource is refused -32010."""
    _declared_resources(ctx)
    try:
        await ctx.client.read_resource("labwire:conformance-no-such-resource")
    except LabwireError as exc:
        if exc.code != -32010:
            raise CheckFailed(
                f"unknown resource read refused with {exc.code}; the spec requires -32010"
            ) from exc
        return ""
    raise CheckFailed("reading an undeclared resource succeeded")


async def check_unknown_reference_refused(ctx: CheckContext) -> str:
    """SPEC 7.2/10.4: a bogus resource_ref value is refused -32010 with a pointer."""
    spec = ctx.first_command(
        lambda s: any(
            isinstance(p, dict) and "resource_ref" in p
            for p in s.params_schema.get("properties", {}).values()
        ),
        "no declared command takes a resource_ref parameter",
    )
    params = ctx.synthesize(spec)
    ref_property = next(
        name
        for name, prop in spec.params_schema.get("properties", {}).items()
        if isinstance(prop, dict) and "resource_ref" in prop and name in params
    )
    broken = copy.deepcopy(params)
    broken[ref_property] = "labwire:conformance/no-such-item"
    exc = await _expect_refusal(
        ctx,
        spec,
        code=-32010,
        category="unknown_reference",
        params=broken,
        confirmation="conformance-standing-confirmation",
    )
    details = exc.details or {}
    if "pointer" not in details:
        raise CheckFailed("the -32010 refusal carries no RFC 6901 pointer in details")
    return ""


async def check_cancel_semantics_declared(ctx: CheckContext) -> str:
    """SPEC 8.3: any declared cancel_semantics is one of the three values."""
    valid = {"abort", "between_steps", "none"}
    problems = [
        f"{raw.get('name', '?')}: {raw.get('cancel_semantics')!r}"
        for raw in ctx.raw_descriptor.get("commands", [])
        if "cancel_semantics" in raw and raw.get("cancel_semantics") not in valid
    ]
    if problems:
        raise CheckFailed("invalid cancel_semantics: " + "; ".join(problems[:3]))
    declared = sum(1 for raw in ctx.raw_descriptor.get("commands", []) if "cancel_semantics" in raw)
    return f"{declared} command(s) declare cancel semantics; the rest default to none"


async def check_cancel_terminal_refused(ctx: CheckContext) -> str:
    """SPEC 8.3: cancelling a terminal run is refused -32007, never accepted.

    Rides the exercised run so nothing extra ever executes; a mid-run
    refusal on a running none command needs a test deployment and is
    proven by the reference test suite instead.
    """
    if ctx.exercised_command_id is None:
        raise Unexercised("rides the exercised run; pass --exercise")
    async with RawWire(ctx.url) as wire:
        await wire.initialize()
        response = await wire.call(
            "command/cancel", {"command_id": ctx.exercised_command_id}, request_id=31
        )
    error = response.get("error")
    if not isinstance(error, dict):
        raise CheckFailed("cancelling a terminal run was accepted")
    if error.get("code") != -32007:
        raise CheckFailed(
            f"cancel of a terminal run refused with {error.get('code')}; SPEC requires -32007"
        )
    details = (error.get("data") or {}).get("details") or {}
    if not details.get("state"):
        raise CheckFailed("the -32007 refusal does not name the run's state in details")
    return ""


# --- streaming -------------------------------------------------------------


async def check_telemetry_subscribe(ctx: CheckContext) -> str:
    """SPEC 9: subscribing to a declared channel opens and closes cleanly."""
    channels = ctx.parsed().channels
    if not channels:
        raise NotApplicable("no telemetry channels are declared")
    async with ctx.client.telemetry([channels[0].name]):
        pass
    return ""


async def check_events_subscribe(ctx: CheckContext) -> str:
    """SPEC 11: the event stream opens and closes cleanly."""
    async with ctx.client.events():
        pass
    return ""


# --- signed ----------------------------------------------------------------


def _find_bundle(bundle_dir: Path, command_id: str) -> Path:
    direct = bundle_dir / command_id
    if (direct / "manifest.json").exists():
        return direct
    for manifest in sorted(bundle_dir.glob("*/manifest.json")):
        try:
            data = json.loads(manifest.read_text())
        except ValueError:
            continue
        if data.get("run_id") == command_id:
            return manifest.parent
    raise CheckFailed(f"no bundle in {bundle_dir} matches the exercised command {command_id!r}")


async def check_bundle_verifies(ctx: CheckContext) -> str:
    """SPEC 13: the exercised run produced a bundle that verifies."""
    if ctx.options.bundle_dir is None:
        raise Unexercised("pass --bundle-dir DIR where the server writes signed run bundles")
    if ctx.exercised_command_id is None:
        raise Unexercised("needs --exercise; the bundle is matched to the exercised run")
    from labwire.core import verify_bundle

    bundle = _find_bundle(ctx.options.bundle_dir, ctx.exercised_command_id)
    outcome = verify_bundle(bundle)
    if not outcome.ok:
        raise CheckFailed(f"bundle {bundle.name} does not verify: {outcome.errors}")
    return f"verified {bundle.name}"


async def check_tamper_detected(ctx: CheckContext) -> str:
    """SPEC 13: a tampered copy of that bundle must fail verification."""
    if ctx.options.bundle_dir is None or ctx.exercised_command_id is None:
        raise Unexercised("needs --exercise and --bundle-dir, same as signed.bundle_verifies")
    import shutil
    import tempfile

    from labwire.core import verify_bundle

    bundle = _find_bundle(ctx.options.bundle_dir, ctx.exercised_command_id)
    with tempfile.TemporaryDirectory() as tmp:
        copied = Path(tmp) / bundle.name
        shutil.copytree(bundle, copied)
        records = copied / "records.jsonl"
        content = bytearray(records.read_bytes()) if records.exists() else bytearray()
        if content:
            # A run with streamed records: flip one bit of the data.
            content[len(content) // 2] ^= 0x01
            records.write_bytes(bytes(content))
            tampered = "a bit-flipped records.jsonl"
        else:
            # No records were streamed; tamper the signed manifest instead.
            manifest_path = copied / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["status"] = "tampered"
            manifest_path.write_text(json.dumps(manifest))
            tampered = "an edited manifest status"
        if verify_bundle(copied).ok:
            raise CheckFailed(f"{tampered} still verifies; the signature binds nothing")
    return ""


@dataclass(frozen=True)
class Check:
    """One registered check."""

    check_id: str
    spec: str
    level: str
    run: Callable[[CheckContext], Awaitable[str]]


CHECKS: tuple[Check, ...] = (
    Check("core.initialize.negotiates_0_4", "SPEC 4, 6.1", "core", check_initialize_negotiates),
    Check("core.initialize.required_first", "SPEC 6.2", "core", check_initialize_required),
    Check("core.jsonrpc.parse_error_recovery", "SPEC 12", "core", check_parse_error_recovery),
    Check("core.jsonrpc.method_not_found", "SPEC 12", "core", check_method_not_found),
    Check("core.ping", "SPEC 6.4", "core", check_ping),
    Check("core.describe.descriptor_valid", "SPEC 7", "core", check_descriptor_valid),
    Check("core.describe.units_mandatory", "SPEC 7.2, 7.3", "core", check_units_mandatory),
    Check("core.errors.unsupported_command", "SPEC 8.2, 12", "core", check_unsupported_command),
    Check("core.errors.validation_refusal", "SPEC 8.2, 12", "core", check_validation_refusal),
    Check("safety.s2.refused_unconfirmed", "SPEC 8.6", "core", check_s2_refused_unconfirmed),
    Check("safety.s3.refused_ungranted", "SPEC 8.6", "core", check_s3_refused_ungranted),
    Check("resources.read_each", "SPEC 10.1, 10.2", "core", check_resources_read),
    Check("resources.unknown_read_refused", "SPEC 10.2", "core", check_unknown_resource_refused),
    Check(
        "references.unknown_ref_refused", "SPEC 7.2, 10.4", "core", check_unknown_reference_refused
    ),
    Check("core.lifecycle.exercise", "SPEC 8", "core", check_lifecycle_exercise),
    Check("cancel.semantics_declared", "SPEC 8.3", "core", check_cancel_semantics_declared),
    Check("cancel.terminal_refused", "SPEC 8.3", "core", check_cancel_terminal_refused),
    Check("streaming.telemetry_subscribe", "SPEC 9", "streaming", check_telemetry_subscribe),
    Check("streaming.events_subscribe", "SPEC 11", "streaming", check_events_subscribe),
    Check("signed.bundle_verifies", "SPEC 13", "signed", check_bundle_verifies),
    Check("signed.tamper_detected", "SPEC 13", "signed", check_tamper_detected),
)
