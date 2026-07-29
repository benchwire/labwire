"""Server SDK: wrap any device in the Labwire protocol (SPEC sections 6-10).

A driver author subclasses :class:`Instrument`, declares commands with
:func:`command`, channels with :func:`channel`, and interlocks with
:func:`interlock`; :class:`InstrumentServer` then speaks the protocol on any
transport.

Example:
    >>> from labwire.core.capabilities import IdentityInfo
    >>> from labwire.core.server import CommandContext, Instrument, command
    >>> class Blinker(Instrument):
    ...     identity = IdentityInfo(
    ...         manufacturer="m", model="d", serial_number="s", firmware_version="1"
    ...     )
    ...     @command()
    ...     async def blink(self, ctx: CommandContext, times: int = 1) -> dict[str, int]:
    ...         '''Blink the indicator light.'''
    ...         return {"blinked": times}
    >>> Blinker().describe().commands[0].name
    'blink'
"""

import asyncio
import copy
import difflib
import hashlib
import inspect
import itertools
import json
import logging
import math
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, ClassVar, Concatenate, NoReturn, Protocol, cast

from labwire.core._meta import PROTOCOL_VERSION, __version__
from labwire.core.capabilities import (
    CancelSemantics,
    ChannelSpec,
    CommandSpec,
    IdentityInfo,
    InstrumentDescriptor,
    InterlockSpec,
    ResourceSpec,
    SafetyClass,
)
from labwire.core.errors import (
    AuthorizationRequiredError,
    BusyError,
    CanceledError,
    ConfirmationRequiredError,
    InterlockError,
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    LabwireError,
    MethodNotFoundError,
    NotCancelableError,
    StaleRevisionError,
    UnknownReferenceError,
    UnsupportedError,
    ValidationError,
)
from labwire.core.grants import GrantStore, GrantVerdict
from labwire.core.jcs import jcs_canonical, params_digest
from labwire.core.messages import (
    TERMINAL_STATES,
    Cancellation,
    CommandIdParams,
    CommandState,
    CommandStatus,
    EventSeverity,
    InitializeParams,
    InitializeResult,
    PeerInfo,
    Progress,
    ResourceIndexEntry,
    ResourceReadParams,
    ResourceReadResult,
    ResourceRevision,
    ServerCapabilities,
    SubmitParams,
    SubscribeParams,
    UnsubscribeParams,
)
from labwire.core.session import JsonRpcSession, SessionClosed
from labwire.core.signing import MANIFEST_VERSION, SigningKey, sign_manifest
from labwire.core.transport import Transport, TransportClosed
from labwire.core.types import JsonRpcError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    create_model,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)
from pydantic_core import to_jsonable_python

logger = logging.getLogger("labwire.server")


def rfc3339(moment: datetime) -> str:
    """Format a UTC datetime as SPEC §9.2 requires.

    Example:
        >>> from datetime import UTC, datetime
        >>> rfc3339(datetime(2026, 7, 23, 15, 30, tzinfo=UTC))
        '2026-07-23T15:30:00.000000Z'
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Clock(Protocol):
    """Injectable time source so simulators can scale time (M3/M6).

    Example:
        >>> class FrozenClock:
        ...     def now(self) -> datetime:
        ...         return datetime(2026, 1, 1, tzinfo=UTC)
        ...     async def sleep(self, seconds: float) -> None:
        ...         pass
    """

    def now(self) -> datetime:
        """Return the current UTC time."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` of instrument time."""
        ...


class SystemClock:
    """Real wall-clock time; the default :class:`Clock`.

    Example:
        >>> SystemClock().now().tzinfo is UTC
        True
    """

    def now(self) -> datetime:
        """Return the current UTC wall-clock time."""
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` of wall-clock time."""
        await asyncio.sleep(seconds)


class TelemetryChannel:
    """A declared measurement channel; publish samples through it.

    Created with :func:`channel` at class scope; each :class:`Instrument`
    instance gets its own copy.

    Example:
        >>> ch = channel("mass", unit="g")
        >>> ch.spec.dtype
        'float64'
    """

    def __init__(self, spec: ChannelSpec) -> None:
        self.spec = spec
        self._seq = 0
        self._publisher: Callable[[TelemetryChannel, int, datetime, Any], None] | None = None
        self._on_invalid: Callable[[TelemetryChannel, str], None] | None = None
        self._clock: Clock = SystemClock()

    def _valid(self, value: Any) -> str | None:
        match self.spec.dtype:
            case "float64":
                if isinstance(value, bool) or not isinstance(value, int | float):
                    return f"expected float64, got {type(value).__name__}"
                if not math.isfinite(value):
                    return "non-finite float64 (SPEC §7.3)"
            case "int64":
                if isinstance(value, bool) or not isinstance(value, int):
                    return f"expected int64, got {type(value).__name__}"
                if abs(value) > 2**53 - 1:
                    return "int64 outside the exactly-representable range (SPEC §7.3)"
            case "bool":
                if not isinstance(value, bool):
                    return f"expected bool, got {type(value).__name__}"
            case "string":
                if not isinstance(value, str):
                    return f"expected string, got {type(value).__name__}"
        return None

    def publish(self, value: Any) -> None:
        """Timestamp, sequence, and fan out one sample to subscribers.

        Sequence numbers increment by exactly 1 per produced sample
        (SPEC §9.2), whether or not anyone is subscribed. Values that
        violate the channel's dtype, including non-finite floats, are
        suppressed and reported as an ``error/occurred`` event (SPEC §7.3);
        they are never produced, so they consume no sequence number.

        Example:
            >>> ch = channel("mass", unit="g")
            >>> ch.publish(12.5)  # no-op until the instrument is served
        """
        problem = self._valid(value)
        if problem is not None:
            if self._on_invalid is not None:
                self._on_invalid(self, problem)
            return
        self._seq += 1
        if self._publisher is not None:
            self._publisher(self, self._seq, self._clock.now(), value)


def channel(
    name: str,
    *,
    unit: str,
    dtype: str = "float64",
    description: str = "",
    qudt_quantity_kind: str | None = None,
    sample_rate_hz_hint: float | None = None,
) -> TelemetryChannel:
    """Declare a typed measurement channel at class scope (SPEC §7.3).

    ``unit`` MUST be a non-empty UCUM code (``"1"`` for dimensionless).

    Example:
        >>> mass = channel("mass", unit="g", description="Current mass.")
    """
    spec = ChannelSpec.model_validate(
        {
            "name": name,
            "description": description or name,
            "dtype": dtype,
            "unit": unit,
            "qudt_quantity_kind": qudt_quantity_kind,
            "sample_rate_hz_hint": sample_rate_hz_hint,
        }
    )
    return TelemetryChannel(spec)


class Interlock:
    """A declared safety interlock; trip/clear it from driver code (SPEC §8.5).

    Example:
        >>> lock = interlock("over_pressure", description="Line pressure high.")
        >>> lock.tripped
        False
    """

    def __init__(self, name: str, description: str, kind: str) -> None:
        self.name = name
        self.description = description
        self.kind = kind
        self._tripped = False
        self._on_change: Callable[[Interlock, bool], None] | None = None

    @property
    def tripped(self) -> bool:
        """Whether the interlock is currently tripped."""
        return self._tripped

    def trip(self) -> None:
        """Trip the interlock: submits are rejected, running commands fail.

        Example:
            >>> lock = interlock("i", description="d")
            >>> lock.trip(); lock.tripped
            True
        """
        if not self._tripped:
            self._tripped = True
            if self._on_change is not None:
                self._on_change(self, True)

    def clear(self) -> None:
        """Clear the interlock; normal operation resumes."""
        if self._tripped:
            self._tripped = False
            if self._on_change is not None:
                self._on_change(self, False)

    def spec(self) -> InterlockSpec:
        """Snapshot as the SPEC §7.4 declaration object."""
        return InterlockSpec.model_validate(
            {
                "name": self.name,
                "description": self.description,
                "kind": self.kind,
                "tripped": self._tripped,
            }
        )


def interlock(name: str, *, description: str, kind: str = "soft") -> Interlock:
    """Declare a safety interlock at class scope (SPEC §7.4).

    Example:
        >>> estop = interlock("estop", description="Emergency stop.", kind="hard")
    """
    return Interlock(name, description, kind)


class CommandContext:
    """Handed to every command handler: progress, events, cancellation, time.

    Example:
        >>> # async def dose(self, ctx: CommandContext, ml: float) -> None:
        >>> #     await ctx.progress(0.5, "half done")
        >>> #     if ctx.cancel_requested: raise CanceledError("stopped")
    """

    def __init__(
        self,
        clock: Clock,
        emit_event: Callable[[str, EventSeverity, dict[str, Any]], None],
        report_progress: Callable[[float | None, str | None], Awaitable[None]],
        cancel_semantics: CancelSemantics = "none",
    ) -> None:
        self._clock = clock
        self._emit_event = emit_event
        self._report_progress = report_progress
        self._cancel_requested = False
        self._cancel_semantics: CancelSemantics = cancel_semantics
        self._boundary_completed = 0
        self._boundary_last: str | None = None
        self._boundary_of: int | None = None
        self._halt_detail: str | None = None
        self._halt_confirmed = False
        self._boundary_stop = False

    @property
    def cancel_requested(self) -> bool:
        """True once ``command/cancel`` has been initiated for this run."""
        return self._cancel_requested

    @property
    def cancel_semantics(self) -> CancelSemantics:
        """What this command declared cancel to mean (SPEC §8.3)."""
        return self._cancel_semantics

    def boundary(self, name: str, of: int | None = None) -> None:
        """Mark a step boundary of a ``between_steps`` command (SPEC §8.3).

        Call it after each backend operation completes. If a cancel has
        been accepted, the run settles here: this raises
        :class:`CanceledError` and the terminal record says the run halted
        at this boundary, naming ``name`` as the last completed step.

        Raises:
            RuntimeError: if the command did not declare
                ``cancel="between_steps"``; that is a driver bug, not a
                runtime condition.

        Example:
            >>> # await do_aspirate(); ctx.boundary("aspirate", of=2)
            >>> # await do_dispense(); ctx.boundary("dispense", of=2)
        """
        if self._cancel_semantics != "between_steps":
            raise RuntimeError(
                "ctx.boundary() is only meaningful for commands declared "
                f"cancel='between_steps'; this one declares {self._cancel_semantics!r}"
            )
        self._boundary_completed += 1
        self._boundary_last = name
        if of is not None:
            self._boundary_of = of
        if self._cancel_requested:
            # Provenance matters: only a stop RAISED HERE may settle as
            # halted_at_boundary. A handler that passes a boundary and then
            # gives up mid-step settles unconfirmed (SPEC 8.3).
            self._boundary_stop = True
            raise CanceledError(f"stopped at boundary after {name!r}")

    def confirm_halted(self, detail: str = "backend confirmed the halt") -> NoReturn:
        """Settle an ``abort`` command's cancel as a CONFIRMED halt.

        Call it only after the backend positively confirmed the physical
        stop; the terminal record then says ``halted``. Raising
        :class:`CanceledError` without calling this settles the honest
        alternative, ``unconfirmed``.

        Raises:
            RuntimeError: if the command did not declare ``cancel="abort"``.
            CanceledError: always, once the halt is recorded.

        Example:
            >>> # if ctx.cancel_requested:
            >>> #     await backend.stop(); await backend.wait_stopped()
            >>> #     ctx.confirm_halted("spindle reports idle")
        """
        if self._cancel_semantics != "abort":
            raise RuntimeError(
                "ctx.confirm_halted() is only meaningful for commands declared "
                f"cancel='abort'; this one declares {self._cancel_semantics!r}"
            )
        if not self._cancel_requested:
            raise RuntimeError(
                "ctx.confirm_halted() with no pending cancel is a driver bug: "
                "there is nothing to settle"
            )
        self._halt_confirmed = True
        self._halt_detail = detail
        raise CanceledError(detail)

    async def progress(self, fraction: float | None = None, message: str | None = None) -> None:
        """Push a ``running`` status notification with progress (SPEC §8.2)."""
        await self._report_progress(fraction, message)

    def emit_event(
        self, name: str, severity: EventSeverity = "info", data: dict[str, Any] | None = None
    ) -> None:
        """Emit a protocol event to every operational session (SPEC §11)."""
        self._emit_event(name, severity, data or {})

    def now(self) -> datetime:
        """Current instrument time (respects the injected clock)."""
        return self._clock.now()

    async def sleep(self, seconds: float) -> None:
        """Sleep in instrument time (respects the injected clock)."""
        await self._clock.sleep(seconds)


class ResourceSnapshot:
    """One read of a resource: its index and its content, together.

    A reader returns both at once so revision derivation covers exactly what
    a client would see, and index and content cannot disagree about a moment.

    Example:
        >>> snap = ResourceSnapshot(index=[], content=None)
        >>> snap.index
        []
    """

    def __init__(self, *, index: list[ResourceIndexEntry], content: Any) -> None:
        self.index = index
        self.content = content


class InstrumentResource:
    """A declared resource (SPEC §7.6); created with :func:`resource`.

    Each :class:`Instrument` instance gets its own copy, like channels. The
    revision is derived from the canonicalized read result, so a driver
    cannot forget to bump it; :meth:`touch` recomputes it and emits the
    reserved ``resource/changed`` event when it moved.

    Example:
        >>> # deck = resource("labwire:deck", kind="deck", ...)
        >>> # @deck.reader
        >>> # def _read_deck(self) -> ResourceSnapshot: ...
    """

    def __init__(
        self,
        uri: str,
        *,
        kind: str,
        title: str,
        description: str,
        content_model: type[BaseModel],
        item_kinds: list[str],
    ) -> None:
        self.uri = uri
        self.kind = kind
        self.title = title
        self.description = description
        self.content_model = content_model
        self.item_kinds = list(item_kinds)
        self._reader_name: str | None = None
        self._owner: Any = None
        self._on_changed: Callable[[str, str], None] | None = None
        self._last_revision: str | None = None
        # Validate the declaration now, so a bad content model fails at class
        # definition time with the same discipline as @command.
        self.content_schema = content_model.model_json_schema(mode="serialization")
        self.content_schema.pop("title", None)
        try:
            self.spec_template = ResourceSpec(
                uri=uri,
                kind=kind,
                title=title,
                description=description,
                item_kinds=self.item_kinds,
                revision="unread",
                content_schema=self.content_schema,
            )
        except PydanticValidationError as exc:
            reasons = "; ".join(
                str(error["msg"]).removeprefix("Value error, ") for error in exc.errors()
            )
            raise TypeError(f"invalid resource declaration {uri!r}: {reasons}") from exc

    def reader(self, fn: Callable[[Any], ResourceSnapshot]) -> Callable[[Any], ResourceSnapshot]:
        """Register the method that produces this resource's snapshot.

        Example:
            >>> # @deck.reader
            >>> # def _read_deck(self) -> ResourceSnapshot: ...
        """
        self._reader_name = fn.__name__
        return fn

    def _bound(self) -> ResourceSnapshot:
        if self._reader_name is None or self._owner is None:
            raise TypeError(f"resource {self.uri!r} has no @reader method")
        snapshot = getattr(self._owner, self._reader_name)()
        if not isinstance(snapshot, ResourceSnapshot):
            raise TypeError(
                f"resource {self.uri!r}: reader must return a ResourceSnapshot, "
                f"got {type(snapshot).__name__}"
            )
        return snapshot

    def read(self, clock: Clock | None = None) -> ResourceReadResult:
        """Read the resource: index, content, and the derived revision.

        Example:
            >>> # instrument.deck.read()
        """
        snapshot = self._bound()
        content = _jsonable(snapshot.content)
        index = [entry.model_dump(mode="json", exclude_none=True) for entry in snapshot.index]
        revision = self._derive_revision(index, content)
        self._last_revision = revision
        moment = (clock or SystemClock()).now()
        return ResourceReadResult(
            uri=self.uri,
            kind=self.kind,
            revision=revision,
            read_at=rfc3339(moment),
            index_complete=True,
            index=[ResourceIndexEntry.model_validate(entry) for entry in index],
            content=content,
        )

    def _derive_revision(self, index: list[dict[str, Any]], content: Any) -> str:
        digest = hashlib.sha256(jcs_canonical({"index": index, "content": content})).hexdigest()
        return digest[:16]

    def revision(self) -> str:
        """The current derived revision (reads the resource).

        Example:
            >>> # instrument.deck.revision()
        """
        snapshot = self._bound()
        content = _jsonable(snapshot.content)
        index = [entry.model_dump(mode="json", exclude_none=True) for entry in snapshot.index]
        return self._derive_revision(index, content)

    def touch(self) -> None:
        """Recompute the revision and emit ``resource/changed`` if it moved.

        Drivers call this after anything that may have changed state; a
        touch that changed nothing emits nothing, so calling it liberally
        is safe.

        Example:
            >>> # self.deck.touch()
        """
        previous = self._last_revision
        current = self.revision()
        self._last_revision = current
        if current != previous and self._on_changed is not None:
            self._on_changed(self.uri, current)

    def spec(self) -> ResourceSpec:
        """The declaration for the descriptor, with a current revision.

        Example:
            >>> # instrument.deck.spec().kind
        """
        return ResourceSpec(
            uri=self.uri,
            kind=self.kind,
            title=self.title,
            description=self.description,
            item_kinds=self.item_kinds,
            revision=self.revision(),
            content_schema=self.content_schema,
        )


def resource(
    uri: str,
    *,
    kind: str,
    title: str,
    description: str,
    content_model: type[BaseModel],
    item_kinds: list[str] | None = None,
) -> InstrumentResource:
    """Declare a resource at class scope (SPEC §7.6), like a channel.

    ``content_model`` is a pydantic model whose serialization schema becomes
    ``content_schema``; its numeric fields must carry ``unit`` keywords via
    ``json_schema_extra`` (see :func:`unit_field`). The declaration is
    validated immediately, with the same import-time discipline as
    :func:`command`.

    Example:
        >>> # deck = resource("labwire:deck", kind="deck", title="Deck",
        >>> #     description="...", content_model=DeckState,
        >>> #     item_kinds=["labware", "container"])
    """
    return InstrumentResource(
        uri,
        kind=kind,
        title=title,
        description=description,
        content_model=content_model,
        item_kinds=item_kinds or [],
    )


def ResourceRef(kind: str, *, enumerated_by: str, description: str | None = None) -> Any:
    """An ``Annotated[str, ...]`` parameter type carrying a typed reference.

    The ``resource_ref`` keyword and a generated description ride inside the
    parameter's own schema (SPEC §7.2), which is the object that travels into
    agent tool schemas, so the pointer to where valid values live reaches the
    agent at the exact parameter it cannot fill. No pattern is emitted:
    patterns are satisfiable by invention.

    Example:
        >>> Container = ResourceRef("container", enumerated_by="labwire:deck")
        >>> # async def transfer(self, ctx, source: Container, ...) -> ...
    """
    article = "an" if kind[0] in "aeiou" else "a"
    sentence = description or (
        f"Must be {article} {kind} listed in the index of resource {enumerated_by}; "
        "read that resource for the valid values."
    )
    return Annotated[
        str,
        Field(
            description=sentence,
            json_schema_extra={"resource_ref": {"kind": kind, "enumerated_by": enumerated_by}},
        ),
    ]


def unit_field(unit_code: str, **kwargs: Any) -> Any:
    """A pydantic ``Field`` whose schema carries the SPEC §7.6 unit keyword.

    Example:
        >>> from pydantic import BaseModel
        >>> class Syringe(BaseModel):
        ...     capacity_ul: float = unit_field("uL")
    """
    extra = dict(kwargs.pop("json_schema_extra", None) or {})
    extra["unit"] = unit_code
    return Field(json_schema_extra=extra, **kwargs)


def _jsonable(value: Any) -> Any:
    """Normalize a handler's return value to plain JSON types.

    A command may return a pydantic model, which is the right way to declare
    a result the protocol can type-check. The wire, the run record, and the
    signed manifest must all carry the same plain JSON, so the conversion
    happens once here rather than being repeated (and diverging) downstream.

    Example:
        >>> _jsonable({"volume_ul": 1.5})
        {'volume_ul': 1.5}
    """
    if value is None or isinstance(value, str | bool | int | float):
        return value
    return to_jsonable_python(value, exclude_none=True)


class CommandMeta(BaseModel):
    """Introspected metadata attached to an ``@command`` handler."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: CommandSpec
    params_model: type[BaseModel]
    attr_name: str


class _CommandFunc(Protocol):
    __labwire_command__: CommandMeta

    def __call__(self, *args: Any, **kwargs: Any) -> Awaitable[Any]: ...


def command[**P, R](
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    units: dict[str, str] | None = None,
    returns_units: dict[str, str] | None = None,
    qudt_quantity_kind: dict[str, str] | None = None,
    safety_class: SafetyClass = "S1",
    cancel: CancelSemantics = "none",
    estimated_duration_s: float | None = None,
    clears_interlocks: list[str] | None = None,
) -> Callable[
    [Callable[Concatenate[Any, CommandContext, P], Awaitable[R]]],
    Callable[Concatenate[Any, CommandContext, P], Awaitable[R]],
]:
    """Declare an instrument command (SPEC §7.2); schema is auto-generated.

    The decorated method keeps its exact signature for direct calls; its
    parameters (after ``self`` and ``ctx``) become the command's
    ``params_schema`` via pydantic, with unknown params **rejected**: an
    agent's typo'd parameter must fail loudly, never silently default.
    ``name`` overrides the wire name (needed for ``x-<vendor>/...``
    extension commands, which are not valid Python identifiers). A
    description (docstring or ``description=``) is required: agents decide
    from it.

    Every numeric parameter MUST have a UCUM code in ``units`` (``"1"`` for
    dimensionless) and every named numeric result field one in
    ``returns_units``; a declaration that omits one raises :class:`TypeError`
    at import time rather than serving an ambiguous descriptor.
    ``safety_class`` (SPEC §8.6, taxonomy from LAP) gates submission:
    ``S2``/``S3`` commands require a confirmation value.

    Example:
        >>> class Valve(Instrument):
        ...     identity = IdentityInfo(
        ...         manufacturer="m", model="d", serial_number="s", firmware_version="1"
        ...     )
        ...     @command(units={"position": "1"}, safety_class="S2")
        ...     async def move(self, ctx: CommandContext, position: float) -> None:
        ...         '''Move the valve.'''
    """

    def deco(
        fn: Callable[Concatenate[Any, CommandContext, P], Awaitable[R]],
    ) -> Callable[Concatenate[Any, CommandContext, P], Awaitable[R]]:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())[2:]  # drop self and ctx
        fields: dict[str, Any] = {}
        for p in params:
            if p.annotation is inspect.Parameter.empty:
                raise TypeError(f"command parameter {p.name!r} needs a type annotation")
            default = ... if p.default is inspect.Parameter.empty else p.default
            fields[p.name] = (p.annotation, default)
        params_model = create_model(
            f"{fn.__name__}_params",
            __config__=ConfigDict(extra="forbid"),
            **fields,
        )
        params_schema = params_model.model_json_schema()
        params_schema.pop("title", None)
        returns_schema: dict[str, Any] | None = None
        if sig.return_annotation not in (inspect.Signature.empty, None, Any):
            # Serialization mode, not validation mode: a @computed_field is
            # omitted from the validation schema by design but is serialized
            # onto the wire, so the validation schema understates the result.
            returns_schema = TypeAdapter(sig.return_annotation).json_schema(mode="serialization")
        resolved_description = description or inspect.getdoc(fn) or ""
        if not resolved_description:
            raise TypeError(
                f"command {fn.__name__!r} needs a docstring or description=, "
                "agents decide when to use a command from its description"
            )
        try:
            spec = CommandSpec(
                name=name or fn.__name__,
                title=title or fn.__name__.replace("_", " ").capitalize(),
                description=resolved_description,
                params_schema=params_schema,
                unit_annotations=units or {},
                returns_units=returns_units or {},
                qudt_quantity_kind=qudt_quantity_kind or {},
                safety_class=safety_class,
                returns_schema=returns_schema,
                estimated_duration_s=estimated_duration_s,
                cancel_semantics=cancel,
                clears_interlocks=clears_interlocks or [],
            )
        except PydanticValidationError as exc:
            # Surface a declaration mistake as a plain TypeError at import time:
            # the driver author, not an agent, is the audience here.
            reasons = "; ".join(
                str(error["msg"]).removeprefix("Value error, ") for error in exc.errors()
            )
            raise TypeError(f"invalid @command declaration on {fn.__name__!r}: {reasons}") from exc
        meta = CommandMeta(spec=spec, params_model=params_model, attr_name=fn.__name__)
        cast("_CommandFunc", fn).__labwire_command__ = meta
        return fn

    return deco


class Instrument:
    """Base class for anything that speaks the Labwire protocol.

    Subclasses set :attr:`identity`, optionally :attr:`max_concurrent_commands`,
    and declare commands/channels/interlocks at class scope. Each instance
    owns independent channel and interlock state.

    Example:
        >>> # class Balance(Instrument): identity = IdentityInfo(...)
    """

    identity: ClassVar[IdentityInfo]
    max_concurrent_commands: ClassVar[int] = 1

    def __init__(self) -> None:
        self._channels: dict[str, TelemetryChannel] = {}
        self._interlocks: dict[str, Interlock] = {}
        self._resources: dict[str, InstrumentResource] = {}
        for klass in reversed(type(self).__mro__):
            for attr_name, attr in vars(klass).items():
                if isinstance(attr, TelemetryChannel):
                    mine = TelemetryChannel(attr.spec)
                    setattr(self, attr_name, mine)
                    self._channels[mine.spec.name] = mine
                elif isinstance(attr, Interlock):
                    mine = copy.copy(attr)
                    mine._tripped = False  # pyright: ignore[reportPrivateUsage]
                    mine._on_change = None  # pyright: ignore[reportPrivateUsage]
                    setattr(self, attr_name, mine)
                    self._interlocks[mine.name] = mine
                elif isinstance(attr, InstrumentResource):
                    own = copy.copy(attr)
                    own._owner = self  # pyright: ignore[reportPrivateUsage]
                    own._on_changed = None  # pyright: ignore[reportPrivateUsage]
                    own._last_revision = None  # pyright: ignore[reportPrivateUsage]
                    setattr(self, attr_name, own)
                    self._resources[own.uri] = own

    def commands(self) -> dict[str, CommandMeta]:
        """Return the declared commands, by name."""
        out: dict[str, CommandMeta] = {}
        for klass in type(self).__mro__:
            for attr in vars(klass).values():
                meta = getattr(attr, "__labwire_command__", None)
                if isinstance(meta, CommandMeta):
                    out.setdefault(meta.spec.name, meta)
        return out

    def describe(self) -> InstrumentDescriptor:
        """Build the SPEC §7.1 descriptor from the declarations.

        Example:
            >>> # Pump().describe().identity.model
        """
        return InstrumentDescriptor(
            identity=self.identity,
            commands=[meta.spec for meta in self.commands().values()],
            channels=[ch.spec for ch in self._channels.values()],
            interlocks=[lock.spec() for lock in self._interlocks.values()],
            resources=[res.spec() for res in self._resources.values()],
            max_concurrent_commands=self.max_concurrent_commands,
        )

    async def on_start(self, server: "InstrumentServer") -> None:
        """Lifecycle hook: the server has started serving this instrument.

        Override to open device connections or spawn background work (e.g.
        idle telemetry) via ``server.spawn(...)``; the default does nothing.

        Example:
            >>> # async def on_start(self, server: InstrumentServer) -> None:
            >>> #     server.spawn(self._stream_idle_mass())
        """

    async def on_stop(self) -> None:
        """Lifecycle hook: the server is shutting down. Default does nothing."""


def _canonical(record: dict[str, Any]) -> bytes:
    return jcs_canonical(record)


def _pydantic_error_details(exc: PydanticValidationError) -> list[dict[str, str]]:
    # Structured, agent-actionable detail (SPEC §12.2): field, message, type.
    # Field names are not internal paths; tracebacks are never included.
    return [
        {
            "field": ".".join(str(part) for part in err["loc"]) or "<root>",
            "message": err["msg"],
            "type": err["type"],
        }
        for err in exc.errors()
    ]


class RunRecord(BaseModel):
    """Immutable public record of one run: the M4 manifest's raw material.

    Example:
        >>> # server.run_records["<command_id>"].status
    """

    run_id: str
    command: str
    params: dict[str, Any]
    safety_class: SafetyClass
    status: CommandState
    result: Any = None
    error: JsonRpcError | None = None
    timestamps: dict[str, str]
    channels: list[str]
    digest: str


class _Run:
    """Server-side state of one command run."""

    def __init__(
        self, run_id: str, meta: CommandMeta, params: dict[str, Any], session: "_ServerSession"
    ) -> None:
        self.run_id = run_id
        self.meta = meta
        self.params = params
        self.session = session
        self.status: CommandState = "accepted"
        self.progress: Progress | None = None
        self.result: Any = None
        self.error: JsonRpcError | None = None
        self.timestamps: dict[str, str] = {}
        self.task: asyncio.Task[None] | None = None
        self.ctx: CommandContext | None = None
        self.fail_reason: LabwireError | None = None
        self.hasher = hashlib.sha256()
        self.channels: set[str] = set()
        self.record_lines: list[bytes] | None = None  # retained iff manifests enabled
        self.authorization: GrantVerdict | None = None
        self.revisions_at_start: dict[str, str] = {}
        self.revisions_at_end: dict[str, str] = {}
        self.cancel_requested_at: str | None = None
        self.cancellation: Cancellation | None = None

    def settle_cancellation(self, outcome: str, detail: str | None = None) -> None:
        """Record what the accepted cancel actually did (SPEC §8.3)."""
        self.cancellation = Cancellation.model_validate(
            {
                "requested_at": self.cancel_requested_at,
                "outcome": outcome,
                "detail": detail,
            }
        )

    def settle_from_context(self, exc_detail: str) -> None:
        """Settle from how the handler ended: boundary, confirmed, or not.

        A between_steps handler that stopped at ctx.boundary() settles
        halted_at_boundary with the boundary recorded; an abort handler
        that called ctx.confirm_halted() settles halted; any other
        CanceledError settles unconfirmed, because nobody confirmed the
        physical state and the record must not pretend otherwise.
        """
        ctx = self.ctx
        if ctx is not None and ctx._boundary_stop:  # pyright: ignore[reportPrivateUsage]
            self.cancellation = Cancellation.model_validate(
                {
                    "requested_at": self.cancel_requested_at,
                    "outcome": "halted_at_boundary",
                    "boundary": {
                        "completed_steps": ctx._boundary_completed,  # pyright: ignore[reportPrivateUsage]
                        "of_steps": ctx._boundary_of,  # pyright: ignore[reportPrivateUsage]
                        "last": ctx._boundary_last,  # pyright: ignore[reportPrivateUsage]
                    },
                    "detail": exc_detail,
                }
            )
        elif ctx is not None and ctx._halt_confirmed:  # pyright: ignore[reportPrivateUsage]
            self.settle_cancellation("halted", detail=ctx._halt_detail)  # pyright: ignore[reportPrivateUsage]
        else:
            self.settle_cancellation("unconfirmed", detail=exc_detail)

    def add_record(self, canonical: bytes) -> None:
        line = canonical + b"\n"
        self.hasher.update(line)
        if self.record_lines is not None:
            self.record_lines.append(line)

    @property
    def active(self) -> bool:
        return self.status not in TERMINAL_STATES

    def is_canceling(self) -> bool:
        # method (not inline compare) because status mutates across tasks,
        # which defeats pyright's flow narrowing at call sites
        return self.status == "canceling"

    def snapshot(self) -> dict[str, Any]:
        changed = [
            ResourceRevision(uri=uri, revision=self.revisions_at_end[uri])
            for uri in sorted(self.revisions_at_end)
            if self.revisions_at_end[uri] != self.revisions_at_start.get(uri)
        ]
        status = CommandStatus(
            command_id=self.run_id,
            status=self.status,
            progress=self.progress,
            result=self.result,
            error=self.error,
            cancellation=self.cancellation if self.status in TERMINAL_STATES else None,
            resource_revisions=changed or None,
        )
        return status.model_dump(mode="json", exclude_none=True)

    def record(self) -> RunRecord:
        return RunRecord(
            run_id=self.run_id,
            command=self.meta.spec.name,
            params=self.params,
            safety_class=self.meta.spec.safety_class,
            status=self.status,
            result=self.result,
            error=self.error,
            timestamps=dict(self.timestamps),
            channels=sorted(self.channels),
            digest=self.hasher.hexdigest(),
        )


class _Subscription:
    """Per-session telemetry subscription with optional rate limiting."""

    def __init__(self, channels: set[str], max_rate_hz: float | None) -> None:
        self.channels = channels
        self.min_interval = 1.0 / max_rate_hz if max_rate_hz else 0.0
        self.last_sent: dict[str, datetime] = {}

    def admit(self, channel_name: str, moment: datetime) -> bool:
        if self.min_interval <= 0.0:
            return True
        last = self.last_sent.get(channel_name)
        if last is not None and (moment - last).total_seconds() < self.min_interval:
            return False  # SPEC §9.1: drop intermediate samples above the ceiling
        self.last_sent[channel_name] = moment
        return True


class _ServerSession:
    """One connected client session on an InstrumentServer."""

    def __init__(self, server: "InstrumentServer", transport: Transport) -> None:
        self.server = server
        self.initialize_received = False
        self.initialized = False
        self.subscriptions: dict[str, _Subscription] = {}
        self.session = JsonRpcSession(
            transport,
            request_handler=self._handle_request,
            notification_handler=self._handle_notification,
        )
        self.session.start()

    async def _handle_request(self, method: str, params: dict[str, Any]) -> Any:
        return await self.server._dispatch(self, method, params)  # pyright: ignore[reportPrivateUsage]

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/initialized" and self.initialize_received:
            self.initialized = True

    def notify_soon(self, method: str, params: dict[str, Any]) -> None:
        """Fire-and-forget a notification; drop the session if it is gone."""

        async def _send() -> None:
            try:
                await self.session.notify(method, params)
            except (SessionClosed, TransportClosed):
                self.server._drop_session(self)  # pyright: ignore[reportPrivateUsage]

        self.server._track(asyncio.create_task(_send()))  # pyright: ignore[reportPrivateUsage]


class InstrumentServer:
    """Host one instrument on any number of transports (SPEC §6.4).

    Example:
        >>> # server = InstrumentServer(SimBalance())
        >>> # server.attach(transport)          # tests / in-process
        >>> # await server.serve_websocket("127.0.0.1", 9520)
    """

    def __init__(
        self,
        instrument: Instrument,
        *,
        clock: Clock | None = None,
        server_name: str = "labwire-server",
        manifest_dir: Path | str | None = None,
        signing_key: SigningKey | None = None,
        confirmation_token: str | None = None,
        grant_store: Path | str | GrantStore | None = None,
    ) -> None:
        self.instrument = instrument
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.server_name = server_name
        self._confirmation_token = confirmation_token
        if isinstance(grant_store, GrantStore):
            self._grant_store: GrantStore | None = grant_store
        elif grant_store is not None:
            self._grant_store = GrantStore(
                Path(grant_store), serial_number=instrument.identity.serial_number
            )
        else:
            env = os.environ.get("LABWIRE_GRANT_STORE")
            self._grant_store = (
                GrantStore(Path(env), serial_number=instrument.identity.serial_number)
                if env
                else None
            )
        declared_s3 = [
            meta.spec.name
            for meta in instrument.commands().values()
            if meta.spec.safety_class == "S3"
        ]
        if declared_s3 and self._grant_store is None:
            # SPEC §6.1: hazardous commands with no way to authorize them is a
            # misconfiguration, not permissiveness.
            raise TypeError(
                f"instrument declares S3 command(s) {sorted(declared_s3)} but no grant "
                "store is configured; pass grant_store= or set LABWIRE_GRANT_STORE"
            )
        self._manifest_dir = Path(manifest_dir) if manifest_dir is not None else None
        self._signing_key = signing_key
        if self._manifest_dir is not None and self._signing_key is None:
            # trust-on-first-use: generate and persist alongside the bundles
            self._signing_key = SigningKey.load_or_generate(self._manifest_dir / "signing.key")
        self._started = False
        self._sessions: set[_ServerSession] = set()
        self._runs: dict[str, _Run] = {}
        self._sub_ids = itertools.count(1)
        self._background: set[asyncio.Task[None]] = set()
        for ch in instrument._channels.values():  # pyright: ignore[reportPrivateUsage]
            ch._publisher = self._publish_sample  # pyright: ignore[reportPrivateUsage]
            ch._on_invalid = self._invalid_sample  # pyright: ignore[reportPrivateUsage]
            ch._clock = self.clock  # pyright: ignore[reportPrivateUsage]
        for lock in instrument._interlocks.values():  # pyright: ignore[reportPrivateUsage]
            lock._on_change = self._interlock_changed  # pyright: ignore[reportPrivateUsage]
        for declared in instrument._resources.values():  # pyright: ignore[reportPrivateUsage]
            declared._on_changed = self._resource_changed  # pyright: ignore[reportPrivateUsage]

    # -- wiring ---------------------------------------------------------------

    async def start(self) -> None:
        """Run the instrument's ``on_start`` hook; idempotent.

        Called automatically when the first client initializes; call it
        directly to start background work before any client connects.

        Example:
            >>> # await server.start()
        """
        if not self._started:
            self._started = True
            await self.instrument.on_start(self)

    async def aclose(self) -> None:
        """Stop serving: cancel background work, close sessions, run ``on_stop``.

        Example:
            >>> # await server.aclose()
        """
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        for session in list(self._sessions):
            await session.session.close()
        self._sessions.clear()
        if self._started:
            self._started = False
            await self.instrument.on_stop()

    def spawn(self, coro: Awaitable[None]) -> None:
        """Run background driver work (idle telemetry, physics loops).

        The task is cancelled by :meth:`aclose`.

        Example:
            >>> # server.spawn(self._physics_loop())   # from Instrument.on_start
        """
        self._track(asyncio.ensure_future(coro))

    def emit_event(
        self, name: str, severity: EventSeverity = "info", data: dict[str, Any] | None = None
    ) -> None:
        """Emit a protocol event outside any command (SPEC §11).

        Example:
            >>> # server.emit_event("instrument/state_changed", "info", {"state": "idle"})
        """
        self._emit_event(name, severity, data or {})

    def attach(self, transport: Transport) -> None:
        """Serve one client session over an already-open transport.

        Example:
            >>> # client_end, server_end = MemoryTransport.pair()
            >>> # server.attach(server_end)
        """
        self._sessions.add(_ServerSession(self, transport))

    def serve_websocket(self, host: str = "127.0.0.1", port: int = 9520) -> Any:
        """Serve over WebSocket (SPEC §5.1); returns an async context manager.

        Example:
            >>> # async with server.serve_websocket("127.0.0.1", 9520):
            >>> #     await asyncio.Future()  # serve forever
        """
        import websockets.asyncio.server as ws_server
        from labwire.core.transport.websocket import WebSocketTransport

        async def handler(connection: Any) -> None:
            transport = WebSocketTransport(connection)
            session = _ServerSession(self, transport)
            self._sessions.add(session)
            try:
                await session.session.wait_closed()
            finally:
                self._drop_session(session)
                await session.session.close()

        return ws_server.serve(handler, host, port)

    @property
    def run_records(self) -> dict[str, RunRecord]:
        """Public snapshot of the runs this server retains (M4 input).

        Terminal runs are evicted when their submitting session closes
        (SPEC §8.2 retention rule).
        """
        return {run_id: run.record() for run_id, run in self._runs.items()}

    def _track(self, task: asyncio.Task[None]) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _drop_session(self, session: _ServerSession) -> None:
        self._sessions.discard(session)
        # SPEC §8.2: terminal statuses are retained until the submitting
        # session closes; evict that session's terminal runs now.
        for run_id in [
            r.run_id
            for r in self._runs.values()
            if r.session is session and r.status in TERMINAL_STATES
        ]:
            del self._runs[run_id]

    # -- dispatch -------------------------------------------------------------

    async def _dispatch(self, session: _ServerSession, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {}
        if method == "initialize":
            return await self._initialize(session, params)
        if not session.initialized:
            raise BusyError("session not initialized", retryable=False)
        match method:
            case "instrument/describe":
                return self.instrument.describe().model_dump(mode="json", exclude_none=True)
            case "resource/read":
                return self._read_resource(params)
            case "command/submit":
                return await self._submit(session, params)
            case "command/status":
                return self._status(params)
            case "command/cancel":
                return self._cancel(params)
            case "telemetry/subscribe":
                return self._subscribe(session, params)
            case "telemetry/unsubscribe":
                return self._unsubscribe(session, params)
            case _:
                raise MethodNotFoundError(f"method not found: {method}")

    async def _initialize(self, session: _ServerSession, params: dict[str, Any]) -> dict[str, Any]:
        if session.initialize_received:
            raise InvalidRequestError("invalid request: initialize after initialization")
        self._validate(InitializeParams, params)
        await self.start()
        session.initialize_received = True
        result = InitializeResult(
            protocol_version=PROTOCOL_VERSION,
            server_info=PeerInfo(name=self.server_name, version=__version__),
            capabilities=ServerCapabilities(
                telemetry=True,
                events=True,
                manifests=self._manifest_dir is not None,
                resources=bool(self.instrument._resources),  # pyright: ignore[reportPrivateUsage]
                grants=self._grant_store is not None,
            ),
        )
        return result.model_dump(mode="json")

    def _validate[M: BaseModel](self, model: type[M], params: dict[str, Any]) -> M:
        # Method-shape params (SPEC §12.1: -32602); the command's own
        # params_schema violations are -32000 and handled in _submit.
        try:
            return model.model_validate(params)
        except PydanticValidationError as exc:
            raise InvalidParamsError(
                f"invalid params for the method: {exc.error_count()} error(s)",
                details={"errors": _pydantic_error_details(exc)},
            ) from exc

    # -- commands -------------------------------------------------------------

    def _active_runs(self) -> list[_Run]:
        return [run for run in self._runs.values() if run.active]

    def _collect_references(
        self, meta: "CommandMeta", normalized: dict[str, Any]
    ) -> list[tuple[str, str, dict[str, str]]]:
        """``(pointer, value, resource_ref)`` for every reference in a submission.

        Walks the schema and the instance together, so the second element of
        an array is nameable by RFC 6901 pointer in the refusal.
        """
        found: list[tuple[str, str, dict[str, str]]] = []

        def walk(node: dict[str, Any], instance: Any, pointer: str) -> None:
            ref = node.get("resource_ref")
            if isinstance(ref, dict) and isinstance(instance, str):
                found.append((pointer, instance, cast("dict[str, str]", ref)))
                return
            reference = node.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                definitions = cast("dict[str, Any]", meta.spec.params_schema.get("$defs") or {})
                target = definitions.get(reference.removeprefix("#/$defs/"))
                if isinstance(target, dict):
                    walk(cast("dict[str, Any]", target), instance, pointer)
                return
            for combinator in ("anyOf", "oneOf", "allOf"):
                variants = node.get(combinator)
                if isinstance(variants, list):
                    for variant in cast("list[Any]", variants):
                        if isinstance(variant, dict):
                            walk(cast("dict[str, Any]", variant), instance, pointer)
            properties = node.get("properties")
            if isinstance(properties, dict) and isinstance(instance, dict):
                for name, member in cast("dict[str, Any]", properties).items():
                    if isinstance(member, dict) and name in instance:
                        walk(
                            cast("dict[str, Any]", member),
                            cast("dict[str, Any]", instance)[name],
                            f"{pointer}/{name}",
                        )
            items = node.get("items")
            if isinstance(items, dict) and isinstance(instance, list):
                for index, element in enumerate(cast("list[Any]", instance)):
                    walk(cast("dict[str, Any]", items), element, f"{pointer}/{index}")
            prefix_items = node.get("prefixItems")
            if isinstance(prefix_items, list) and isinstance(instance, list):
                for index, (member, element) in enumerate(
                    zip(cast("list[Any]", prefix_items), cast("list[Any]", instance), strict=False)
                ):
                    if isinstance(member, dict):
                        walk(cast("dict[str, Any]", member), element, f"{pointer}/{index}")

        walk(meta.spec.params_schema, normalized, "")
        return found

    def _resolve_references(self, meta: "CommandMeta", normalized: dict[str, Any]) -> None:
        """SPEC §10.4: every reference resolves against a fresh read, or refuse."""
        reads: dict[str, ResourceReadResult] = {}
        for pointer, value, ref in self._collect_references(meta, normalized):
            enumerated_by = ref["enumerated_by"]
            expected_kind = ref["kind"]
            source = self.instrument._resources.get(enumerated_by)  # pyright: ignore[reportPrivateUsage]
            if source is None:  # closure makes this unreachable when served
                raise UnknownReferenceError(
                    f"parameter {pointer}: enumerating resource {enumerated_by!r} is not "
                    "declared by this instrument",
                    details={"pointer": pointer, "reference": value, "reason": "unknown_resource"},
                )
            if enumerated_by not in reads:
                reads[enumerated_by] = source.read(self.clock)
            snapshot = reads[enumerated_by]
            resolved_kinds, resolved_prefix, reason = self._resolve_one(snapshot, value)
            if resolved_kinds is not None and expected_kind in resolved_kinds:
                continue
            if resolved_kinds is not None:
                reason = "kind_mismatch"
            parameter = pointer.strip("/").split("/", 1)[0]
            candidates = [entry.uri for entry in snapshot.index if expected_kind in entry.kinds] + [
                f"{entry.uri}/{item_id}"
                for entry in snapshot.index
                if entry.children is not None and expected_kind in entry.children.kinds
                for item_id in entry.children.ids
            ]
            close = difflib.get_close_matches(value, candidates, n=5, cutoff=0.4)
            details: dict[str, Any] = {
                "pointer": pointer,
                "parameter": parameter,
                "reference": value,
                "expected_kind": expected_kind,
                "enumerated_by": enumerated_by,
                "reason": reason,
                "read": {"method": "resource/read", "params": {"uri": enumerated_by}},
            }
            if resolved_prefix is not None:
                details["resolved_prefix"] = resolved_prefix
            if resolved_kinds is not None:
                details["resolved_kinds"] = list(resolved_kinds)
            if close:
                details["did_you_mean"] = close
            article = "a" if expected_kind[0] not in "aeiou" else "an"
            raise UnknownReferenceError(
                f"parameter {pointer}: {value!r} is not {article} {expected_kind} on this "
                "instrument",
                details=details,
            )

    @staticmethod
    def _resolve_one(
        snapshot: ResourceReadResult, value: str
    ) -> tuple[list[str] | None, str | None, str]:
        """Resolve one reference per SPEC §10.2: kinds, longest prefix, reason."""
        if not value.startswith("labwire:") or value.endswith("/") or "//" in value:
            return None, None, "malformed_uri"
        prefix: str | None = None
        for entry in snapshot.index:
            if entry.uri == value:
                return list(entry.kinds), entry.uri, "no_such_item"
            if value.startswith(entry.uri + "/"):
                prefix = entry.uri
                item_id = value.removeprefix(entry.uri + "/")
                if entry.children is not None and item_id in entry.children.ids:
                    return list(entry.children.kinds), entry.uri, "no_such_item"
        if prefix is not None:
            return None, prefix, "no_such_item"
        return None, None, "unknown_resource"

    def _check_revisions(self, if_revision: dict[str, str]) -> None:
        """SPEC §10.5: refuse a stale plan before anything is spent."""
        for uri, submitted in if_revision.items():
            declared = self.instrument._resources.get(uri)  # pyright: ignore[reportPrivateUsage]
            if declared is None:
                raise UnknownReferenceError(
                    f"if_revision names {uri!r}, which this instrument does not declare",
                    details={"reference": uri, "reason": "unknown_resource"},
                )
            current = declared.revision()
            if current != submitted:
                raise StaleRevisionError(
                    f"{uri} has moved since this plan was made",
                    details={
                        "uri": uri,
                        "submitted_revision": submitted,
                        "current_revision": current,
                        "read": {"method": "resource/read", "params": {"uri": uri}},
                    },
                )

    def _authorize_s3(
        self, meta: "CommandMeta", submit: SubmitParams, normalized: dict[str, Any]
    ) -> GrantVerdict:
        """SPEC §8.6: verify and atomically consume a grant, or refuse."""
        digest = params_digest(normalized)
        if self._grant_store is None:  # pragma: no cover - refused at construction
            raise AuthorizationRequiredError(
                f"command {submit.command!r} is S3 and this server holds no grant store",
                details={
                    "safety_class": "S3",
                    "command": submit.command,
                    "reason": "absent",
                    "mintable_by_agent": False,
                },
            )
        if submit.authorization is None:
            pending = self._grant_store.record_pending(
                command=submit.command,
                params=normalized,
                params_digest=digest,
                now=self.clock.now(),
            )
            raise AuthorizationRequiredError(
                f"{submit.command} is S3 and requires an operator grant bound to these "
                "exact parameters; a confirmation string cannot authorize it",
                details={
                    "safety_class": "S3",
                    "command": submit.command,
                    "reason": "absent",
                    "request_id": pending.request_id,
                    "params_digest": digest,
                    "digest_alg": "sha256",
                    "canonicalization": "RFC8785",
                    "mintable_by_agent": False,
                    "operator_instruction": (
                        "On the instrument host run: labwire grant list, then "
                        f"labwire grant approve {pending.request_id} --ttl 15m --uses 1"
                    ),
                },
            )
        verdict = self._grant_store.verify_and_consume(
            grant_id=submit.authorization.grant_id,
            command=submit.command,
            params_digest=digest,
            now=self.clock.now(),
        )
        if not verdict.ok:
            details: dict[str, Any] = {
                "safety_class": "S3",
                "command": submit.command,
                "reason": verdict.reason,
                "params_digest": digest,
                "digest_alg": "sha256",
                "canonicalization": "RFC8785",
                "mintable_by_agent": False,
            }
            raise AuthorizationRequiredError(
                f"the presented grant does not authorize this call: {verdict.reason}",
                details=details,
            )
        return verdict

    async def _submit(self, session: _ServerSession, params: dict[str, Any]) -> dict[str, Any]:
        # Rejection precedence per SPEC §12.1: unsupported → validation →
        # unknown_reference → stale_revision → interlock → capacity busy →
        # confirmation / authorization. Everything knowable without an
        # operator is checked first, so an agent is never asked to confirm,
        # and a single-use grant is never spent, on a call that could not run.
        submit = self._validate(SubmitParams, params)
        meta = self.instrument.commands().get(submit.command)
        if meta is None:
            raise UnsupportedError(f"command not declared: {submit.command}")
        try:
            validated = meta.params_model.model_validate(submit.params)
        except PydanticValidationError as exc:
            raise ValidationError(
                f"params for {submit.command!r} failed validation",
                details={"errors": _pydantic_error_details(exc)},
            ) from exc
        # One object is validated, executed, digested, and recorded (SPEC §8.2):
        # the normalized params, with schema defaults applied.
        normalized = validated.model_dump(mode="json", by_alias=True)
        self._resolve_references(meta, normalized)
        if submit.if_revision:
            self._check_revisions(submit.if_revision)
        tripped = {
            lock.name
            for lock in self.instrument._interlocks.values()  # pyright: ignore[reportPrivateUsage]
            if lock.tripped
        }
        # SPEC §8.5/§8.6: a command clearing ANY currently tripped interlock stays
        # submittable (otherwise two tripped soft interlocks deadlock recovery), and
        # S0 commands stay submittable because they are the means of recovery.
        if (
            tripped
            and meta.spec.safety_class != "S0"
            and not (tripped & set(meta.spec.clears_interlocks))
        ):
            raise InterlockError(f"interlock tripped: {', '.join(sorted(tripped))}")
        if len(self._active_runs()) >= self.instrument.max_concurrent_commands:
            raise BusyError(
                f"at capacity: {self.instrument.max_concurrent_commands} command slot(s) in use"
            )
        authorization: GrantVerdict | None = None
        if meta.spec.safety_class == "S3":
            # A confirmation MUST NOT satisfy S3, whatever it contains (SPEC §8.6).
            authorization = self._authorize_s3(meta, submit, normalized)
        elif meta.spec.safety_class == "S2" and not self._confirmed(submit.confirmation):
            raise ConfirmationRequiredError(
                f"command {submit.command!r} is S2 and requires an operator confirmation value",
                details={"safety_class": meta.spec.safety_class},
            )
        run = _Run(str(uuid.uuid4()), meta, normalized, session)
        run.authorization = authorization
        run.revisions_at_start = self._resource_revisions()
        if self._manifest_dir is not None:
            run.record_lines = []
        run.timestamps["submitted"] = rfc3339(self.clock.now())
        self._runs[run.run_id] = run
        kwargs = {name: getattr(validated, name) for name in type(validated).model_fields}
        run.task = asyncio.create_task(self._execute(run, kwargs))
        run.task.add_done_callback(lambda _task, r=run: self._reap_run(r))
        self._track(run.task)
        return {"command_id": run.run_id, "status": "accepted"}

    def _read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        parsed = self._validate(ResourceReadParams, params)
        declared = self.instrument._resources.get(parsed.uri)  # pyright: ignore[reportPrivateUsage]
        if declared is None:
            reason = (
                "malformed_uri" if not parsed.uri.startswith("labwire:") else "unknown_resource"
            )
            known = sorted(self.instrument._resources)  # pyright: ignore[reportPrivateUsage]
            raise UnknownReferenceError(
                f"{parsed.uri!r} is not a resource this instrument declares"
                + (f"; declared: {', '.join(known)}" if known else ""),
                details={"reference": parsed.uri, "reason": reason},
            )
        return declared.read(self.clock).model_dump(mode="json", exclude_none=True)

    def _resource_changed(self, uri: str, revision: str) -> None:
        self._emit_event("resource/changed", "info", {"uri": uri, "revision": revision})

    def _resource_revisions(self) -> dict[str, str]:
        """Every declared resource's current revision, for run bracketing."""
        return {
            uri: declared.revision()
            for uri, declared in self.instrument._resources.items()  # pyright: ignore[reportPrivateUsage]
        }

    def _confirmed(self, confirmation: str | None) -> bool:
        """Whether a submitted confirmation value is acceptable (SPEC §8.6).

        Deployment policy: with a token configured the value must match it;
        without one, any non-empty value is accepted. Either way this proves
        deployment policy, not operator identity, see SPEC §14.
        """
        if confirmation is None or not confirmation.strip():
            return False
        if self._confirmation_token is None:
            return True
        return confirmation == self._confirmation_token

    def _reap_run(self, run: _Run) -> None:
        # Safety net: a run task must never die leaving the run non-terminal
        # (that would leak a capacity slot forever). Normal paths finish the
        # run inside _execute; this catches externally-cancelled tasks.
        if run.active:
            run.timestamps.setdefault("started", rfc3339(self.clock.now()))
            if run.fail_reason is not None:
                if run.cancel_requested_at is not None and run.cancellation is None:
                    run.settle_cancellation(
                        "unconfirmed", detail="superseded by an interlock abort"
                    )
                run.error = run.fail_reason.to_wire()
                self._finish(run, "failed")
            else:
                if run.cancellation is None:
                    # SPEC 8.3: no canceled terminal without a block. A reaped
                    # task that never ran settles never_started; one that was
                    # running settles unconfirmed.
                    if run.status == "accepted":
                        run.settle_cancellation(
                            "never_started", detail="task reaped before the handler ran"
                        )
                    else:
                        run.settle_cancellation(
                            "unconfirmed", detail="run task ended without settlement"
                        )
                self._cancel_terminal(run)

    async def _execute(self, run: _Run, kwargs: dict[str, Any]) -> None:
        ctx = CommandContext(
            self.clock,
            self._emit_event,
            lambda fraction, message: self._report_progress(run, fraction, message),
            cancel_semantics=run.meta.spec.cancel_semantics,
        )
        run.ctx = ctx
        if run.is_canceling():  # canceled before it ever started
            run.timestamps["started"] = rfc3339(self.clock.now())  # degenerate window
            run.settle_cancellation(
                "never_started", detail="canceled before start; no operation was issued"
            )
            self._finish(run, "canceled")
            return
        run.timestamps["started"] = rfc3339(self.clock.now())
        self._transition(run, "running")
        try:
            bound = getattr(self.instrument, run.meta.attr_name)
            result = await bound(ctx, **kwargs)
        except CanceledError as exc:
            # Handler-initiated cancellation with no wire cancel still ends
            # canceled, and SPEC 8.3 demands a block on EVERY canceled
            # terminal; with no boundary and no confirmed halt it settles
            # unconfirmed, requested_at null.
            run.settle_from_context(str(exc))
            self._cancel_terminal(run)
        except asyncio.CancelledError:
            if run.fail_reason is not None:
                if run.cancel_requested_at is not None:
                    # An interlock abort superseded an accepted cancel; the
                    # cancel settled nothing (SPEC 8.3).
                    run.settle_cancellation(
                        "unconfirmed", detail="superseded by an interlock abort"
                    )
                run.error = run.fail_reason.to_wire()
                self._finish(run, "failed")
            else:
                # External termination (server shutdown): the run ends
                # canceled whether or not anyone asked, and the block says
                # nobody confirmed anything.
                run.settle_cancellation(
                    "unconfirmed", detail="run task was terminated before settlement"
                )
                self._cancel_terminal(run)
        except LabwireError as exc:
            if run.cancel_requested_at is not None:
                # The run concluded on its own terms (a deliberate, typed
                # failure) while a cancel was pending.
                run.settle_cancellation("ran_to_completion")
            run.error = exc.to_wire()
            self._finish(run, "failed")
        except Exception:
            logger.exception("command handler %r crashed (run %s)", run.meta.spec.name, run.run_id)
            if run.cancel_requested_at is not None:
                run.settle_cancellation(
                    "unconfirmed", detail="handler crashed while a cancel was pending"
                )
            run.error = InternalError("internal server error").to_wire()
            self._finish(run, "failed")
        else:
            if run.is_canceling():
                # SPEC 8.3: completion won the race. The terminal status is
                # succeeded, and the cancellation block says a cancel was
                # pending; reporting canceled would erase a completed action.
                run.settle_cancellation("ran_to_completion")
            try:
                run.result = _jsonable(result)
            except Exception:
                logger.exception(
                    "result of %r is not serializable (run %s)", run.meta.spec.name, run.run_id
                )
                run.error = InternalError("command result was not serializable").to_wire()
                self._finish(run, "failed")
            else:
                self._finish(run, "succeeded")

    def _cancel_terminal(self, run: _Run) -> None:
        # SPEC §8.1: `canceled` is reachable only from `canceling`: a
        # spontaneous CanceledError from `running` interposes the transition.
        if not run.is_canceling():
            self._transition(run, "canceling")
        self._finish(run, "canceled")

    def _transition(self, run: _Run, status: CommandState) -> None:
        run.status = status
        run.session.notify_soon("notifications/command_status", run.snapshot())

    def _finish(self, run: _Run, status: CommandState) -> None:
        run.timestamps["completed"] = rfc3339(self.clock.now())
        run.progress = None  # progress is a running-state concept (SPEC §8.2)
        run.revisions_at_end = self._resource_revisions()
        for uri, revision in run.revisions_at_end.items():
            if revision != run.revisions_at_start.get(uri):
                declared = self.instrument._resources.get(uri)  # pyright: ignore[reportPrivateUsage]
                if declared is not None:
                    declared._last_revision = revision  # pyright: ignore[reportPrivateUsage]
                    self._resource_changed(uri, revision)
        self._transition(run, status)
        if self._manifest_dir is not None and self._signing_key is not None:
            try:
                self._write_bundle(run)
            except Exception:
                logger.exception("failed to write manifest bundle for run %s", run.run_id)

    def _write_bundle(self, run: _Run) -> None:
        manifest: dict[str, Any] = {
            "manifest_version": MANIFEST_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "run_id": run.run_id,
            "instrument": self.instrument.identity.model_dump(mode="json", exclude_none=True),
            "command": {
                "name": run.meta.spec.name,
                "params": run.params,
                "safety_class": run.meta.spec.safety_class,
                "cancel_semantics": run.meta.spec.cancel_semantics,
                "params_digest": params_digest(run.params),
            },
            "status": run.status,
            "data": {
                "digest_alg": "sha256",
                "digest": run.hasher.hexdigest(),
                "channels": sorted(run.channels),
            },
            "timestamps": dict(run.timestamps),
        }
        if run.status == "succeeded" and run.result is not None:
            manifest["result"] = run.result
        if run.error is not None:
            manifest["error"] = run.error.model_dump(mode="json", exclude_none=True)
        if run.cancellation is not None:
            manifest["cancellation"] = run.cancellation.model_dump(mode="json", exclude_none=True)
        if run.meta.spec.safety_class == "S2":
            manifest["authorization"] = {"mode": "confirmation", "identity_verified": False}
        elif run.meta.spec.safety_class == "S3" and run.authorization is not None:
            grant = run.authorization.grant
            assert grant is not None
            block: dict[str, Any] = {
                "mode": "grant",
                # The id is a bearer value and a signed bundle is durable, so
                # only its digest is recorded (SPEC §13.1).
                "grant_digest": "sha256:" + hashlib.sha256(grant.grant_id.encode()).hexdigest(),
                "expires_at": grant.expires_at,
                "use_index": run.authorization.use_index,
                "identity_verified": False,
            }
            for label, value in (
                ("request_id", grant.request_id),
                ("issued_by", grant.issued_by),
                ("note", grant.note),
            ):
                if value is not None:
                    block[label] = value
            manifest["authorization"] = block
        changed = {
            uri: revision
            for uri, revision in run.revisions_at_end.items()
            if revision != run.revisions_at_start.get(uri)
        }
        if changed:
            manifest["resource_revisions"] = [
                {
                    "uri": uri,
                    "revision_at_start": run.revisions_at_start.get(uri, ""),
                    "revision_at_end": revision,
                }
                for uri, revision in sorted(changed.items())
            ]
        assert self._manifest_dir is not None
        assert self._signing_key is not None
        doc = sign_manifest(manifest, self._signing_key)
        bundle = self._manifest_dir / run.run_id
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "records.jsonl").write_bytes(b"".join(run.record_lines or []))
        (bundle / "manifest.json").write_text(json.dumps(doc, indent=2) + "\n")

    async def _report_progress(
        self, run: _Run, fraction: float | None, message: str | None
    ) -> None:
        run.progress = Progress(fraction=fraction, message=message)
        run.session.notify_soon("notifications/command_status", run.snapshot())

    def _get_run(self, params: dict[str, Any]) -> _Run:
        # -32602 for a malformed/missing command_id (method shape, SPEC §12.1);
        # -32000 for a well-formed but unknown one (unknown entity).
        parsed = self._validate(CommandIdParams, params)
        run = self._runs.get(parsed.command_id)
        if run is None:
            raise ValidationError(f"unknown command_id: {parsed.command_id!r}")
        return run

    def _status(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._get_run(params).snapshot()

    def _cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self._get_run(params)
        if run.status in TERMINAL_STATES or run.status == "canceling":
            raise NotCancelableError(f"run is {run.status}", details={"state": run.status})
        semantics = run.meta.spec.cancel_semantics
        if run.status == "running" and semantics == "none":
            # SPEC 8.3: refusal is the only honest answer. Accepting and
            # ignoring would report a cancel the hardware never saw.
            raise NotCancelableError(
                f"command {run.meta.spec.name!r} is running and declares "
                "cancel_semantics 'none': the operation is already committed",
                details={"cancel_semantics": "none", "state": "running"},
            )
        run.cancel_requested_at = rfc3339(self.clock.now())
        if run.ctx is not None:
            run.ctx._cancel_requested = True  # pyright: ignore[reportPrivateUsage]
        self._transition(run, "canceling")
        return run.snapshot()

    # -- telemetry and events -------------------------------------------------

    def _subscribe(self, session: _ServerSession, params: dict[str, Any]) -> dict[str, Any]:
        sub = self._validate(SubscribeParams, params)
        unknown = [name for name in sub.channels if name not in self.instrument._channels]  # pyright: ignore[reportPrivateUsage]
        if unknown:
            raise ValidationError(f"unknown channel(s): {', '.join(unknown)}")
        subscription_id = f"sub-{next(self._sub_ids)}"
        session.subscriptions[subscription_id] = _Subscription(set(sub.channels), sub.max_rate_hz)
        return {"subscription_id": subscription_id}

    def _unsubscribe(self, session: _ServerSession, params: dict[str, Any]) -> dict[str, Any]:
        unsub = self._validate(UnsubscribeParams, params)
        if session.subscriptions.pop(unsub.subscription_id, None) is None:
            raise ValidationError(f"unknown subscription_id: {unsub.subscription_id!r}")
        return {}

    def _publish_sample(
        self, chan: TelemetryChannel, seq: int, moment: datetime, value: Any
    ) -> None:
        name = chan.spec.name
        timestamp = rfc3339(moment)
        for run in self._active_runs():
            if run.status != "accepted":
                run.channels.add(name)
                run.add_record(
                    _canonical(
                        {
                            "type": "sample",
                            "channel": name,
                            "seq": seq,
                            "timestamp": timestamp,
                            "value": value,
                        }
                    )
                )
        for session in list(self._sessions):
            if not session.initialized:
                continue
            for subscription_id, subscription in session.subscriptions.items():
                if name in subscription.channels and subscription.admit(name, moment):
                    session.notify_soon(
                        "notifications/telemetry",
                        {
                            "subscription_id": subscription_id,
                            "channel": name,
                            "seq": seq,
                            "timestamp": timestamp,
                            "value": value,
                        },
                    )

    def _invalid_sample(self, chan: TelemetryChannel, problem: str) -> None:
        self._emit_event(
            "error/occurred",
            "warning",
            {"channel": chan.spec.name, "reason": f"sample suppressed: {problem}"},
        )

    def _emit_event(self, name: str, severity: EventSeverity, data: dict[str, Any]) -> None:
        params = {
            "name": name,
            "timestamp": rfc3339(self.clock.now()),
            "severity": severity,
            "data": data,
        }
        for run in self._active_runs():
            if run.status != "accepted":
                run.add_record(
                    _canonical(
                        {
                            "type": "event",
                            "name": name,
                            "timestamp": params["timestamp"],
                            "severity": severity,
                            "data": data,
                        }
                    )
                )
        for session in list(self._sessions):
            if session.initialized:
                session.notify_soon("notifications/event", params)

    def _interlock_changed(self, lock: Interlock, tripped: bool) -> None:
        self._emit_event(
            "interlock/tripped" if tripped else "interlock/cleared",
            "alarm" if tripped else "info",
            {"interlock": lock.name},
        )
        if tripped:
            for run in self._active_runs():
                run.fail_reason = InterlockError(f"interlock tripped: {lock.name}")
                if run.status == "accepted":
                    # SPEC §8.5: fail synchronously, cancelling a not-yet-started
                    # task would skip _execute entirely and leak the slot forever.
                    run.error = run.fail_reason.to_wire()
                    run.timestamps.setdefault("started", rfc3339(self.clock.now()))
                    self._finish(run, "failed")
                if run.task is not None and not run.task.done():
                    run.task.cancel()
