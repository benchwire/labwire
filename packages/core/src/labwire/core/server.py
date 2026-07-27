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
import hashlib
import inspect
import itertools
import json
import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Concatenate, Protocol, cast

from labwire.core._meta import PROTOCOL_VERSION, __version__
from labwire.core.capabilities import (
    CONFIRMATION_REQUIRED_CLASSES,
    ChannelSpec,
    CommandSpec,
    IdentityInfo,
    InstrumentDescriptor,
    InterlockSpec,
    SafetyClass,
)
from labwire.core.errors import (
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
    UnsupportedError,
    ValidationError,
)
from labwire.core.jcs import jcs_canonical
from labwire.core.messages import (
    TERMINAL_STATES,
    CommandIdParams,
    CommandState,
    CommandStatus,
    EventSeverity,
    InitializeParams,
    InitializeResult,
    PeerInfo,
    Progress,
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
    ) -> None:
        self._clock = clock
        self._emit_event = emit_event
        self._report_progress = report_progress
        self._cancel_requested = False

    @property
    def cancel_requested(self) -> bool:
        """True once ``command/cancel`` has been initiated for this run."""
        return self._cancel_requested

    async def progress(self, fraction: float | None = None, message: str | None = None) -> None:
        """Push a ``running`` status notification with progress (SPEC §8.2)."""
        await self._report_progress(fraction, message)

    def emit_event(
        self, name: str, severity: EventSeverity = "info", data: dict[str, Any] | None = None
    ) -> None:
        """Emit a protocol event to every operational session (SPEC §10)."""
        self._emit_event(name, severity, data or {})

    def now(self) -> datetime:
        """Current instrument time (respects the injected clock)."""
        return self._clock.now()

    async def sleep(self, seconds: float) -> None:
        """Sleep in instrument time (respects the injected clock)."""
        await self._clock.sleep(seconds)


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
    interruptible: bool = True,
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
            returns_schema = TypeAdapter(sig.return_annotation).json_schema()
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
                interruptible=interruptible,
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
    # Structured, agent-actionable detail (SPEC §11.2): field, message, type.
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
        status = CommandStatus(
            command_id=self.run_id,
            status=self.status,
            progress=self.progress,
            result=self.result,
            error=self.error,
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
    ) -> None:
        self.instrument = instrument
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.server_name = server_name
        self._confirmation_token = confirmation_token
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
        """Emit a protocol event outside any command (SPEC §10).

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
                telemetry=True, events=True, manifests=self._manifest_dir is not None
            ),
        )
        return result.model_dump(mode="json")

    def _validate[M: BaseModel](self, model: type[M], params: dict[str, Any]) -> M:
        # Method-shape params (SPEC §11.1: -32602); the command's own
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

    async def _submit(self, session: _ServerSession, params: dict[str, Any]) -> dict[str, Any]:
        # Rejection precedence per SPEC §11.1: unsupported → validation →
        # interlock → capacity busy.
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
        if meta.spec.safety_class in CONFIRMATION_REQUIRED_CLASSES and not self._confirmed(
            submit.confirmation
        ):
            raise ConfirmationRequiredError(
                f"command {submit.command!r} is {meta.spec.safety_class} and requires "
                "an operator confirmation value",
                details={"safety_class": meta.spec.safety_class},
            )
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
        run = _Run(str(uuid.uuid4()), meta, submit.params, session)
        if self._manifest_dir is not None:
            run.record_lines = []
        run.timestamps["submitted"] = rfc3339(self.clock.now())
        self._runs[run.run_id] = run
        kwargs = {name: getattr(validated, name) for name in type(validated).model_fields}
        run.task = asyncio.create_task(self._execute(run, kwargs))
        run.task.add_done_callback(lambda _task, r=run: self._reap_run(r))
        self._track(run.task)
        return {"command_id": run.run_id, "status": "accepted"}

    def _confirmed(self, confirmation: str | None) -> bool:
        """Whether a submitted confirmation value is acceptable (SPEC §8.6).

        Deployment policy: with a token configured the value must match it;
        without one, any non-empty value is accepted. Either way this proves
        deployment policy, not operator identity, see SPEC §13.
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
                run.error = run.fail_reason.to_wire()
                self._finish(run, "failed")
            else:
                self._cancel_terminal(run)

    async def _execute(self, run: _Run, kwargs: dict[str, Any]) -> None:
        ctx = CommandContext(
            self.clock,
            self._emit_event,
            lambda fraction, message: self._report_progress(run, fraction, message),
        )
        run.ctx = ctx
        if run.is_canceling():  # canceled before it ever started
            run.timestamps["started"] = rfc3339(self.clock.now())  # degenerate window
            self._finish(run, "canceled")
            return
        run.timestamps["started"] = rfc3339(self.clock.now())
        self._transition(run, "running")
        try:
            bound = getattr(self.instrument, run.meta.attr_name)
            result = await bound(ctx, **kwargs)
        except CanceledError:
            self._cancel_terminal(run)
        except asyncio.CancelledError:
            if run.fail_reason is not None:
                run.error = run.fail_reason.to_wire()
                self._finish(run, "failed")
            else:
                self._cancel_terminal(run)
        except LabwireError as exc:
            run.error = exc.to_wire()
            self._finish(run, "failed")
        except Exception:
            logger.exception("command handler %r crashed (run %s)", run.meta.spec.name, run.run_id)
            run.error = InternalError("internal server error").to_wire()
            self._finish(run, "failed")
        else:
            if run.is_canceling():
                self._finish(run, "canceled")
            else:
                run.result = _jsonable(result)
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
        # -32602 for a malformed/missing command_id (method shape, SPEC §11.1);
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
            raise NotCancelableError(f"run is {run.status}")
        if run.status == "running" and not run.meta.spec.interruptible:
            raise NotCancelableError(f"command {run.meta.spec.name!r} is not interruptible")
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
