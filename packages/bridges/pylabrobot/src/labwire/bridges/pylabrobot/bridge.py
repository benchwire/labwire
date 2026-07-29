"""Serve a live PyLabRobot liquid handler through the Labwire protocol.

:func:`PyLabRobotInstrument` builds a real :class:`labwire.core.Instrument`,
so every v0.2 rule applies to a bridged liquid handler exactly as it does to a
native instrument: UCUM units on every quantity, safety-class confirmation
before anything moves, the command lifecycle, signed run manifests.

Two things differ from the ophyd bridge, both in this module's favour.
PyLabRobot is natively async, so nothing runs in a worker thread. And its
command surface is one fixed class rather than a per-device structure, so the
commands below are written out plainly instead of generated.

The bridge enables PyLabRobot's tip and volume trackers when it builds an
instrument. Without them a liquid handler silently accepts physically
impossible commands, such as aspirating with no tip, which is not a thing to
hand an agent. PyLabRobot toggles both through module-level globals rather
than per-handler state, so this affects every liquid handler in the process.
That is a real side effect and it is in LIMITATIONS, not a footnote.

Example:
    >>> # instrument = PyLabRobotInstrument(lh, load_annotations(path))
    >>> # await InstrumentServer(instrument).serve_websocket("127.0.0.1", 9520)
"""

import functools
from collections.abc import Awaitable
from typing import Any, cast

from labwire.bridges.pylabrobot.addressing import DECK_URI, resolve, resolve_all
from labwire.bridges.pylabrobot.annotations import AnnotationFile, check
from labwire.bridges.pylabrobot.deck import DeckState, deck_snapshot, locked_labware
from labwire.bridges.pylabrobot.introspect import command_surface, introspect
from labwire.core import (
    CommandContext,
    HardwareFaultError,
    Instrument,
    InterlockError,
    LabwireError,
    ResourceRef,
    ResourceSnapshot,
    ValidationError,
    channel,
    command,
    resource,
)
from pydantic import BaseModel, ConfigDict

_POLL_S = 0.02

Container = ResourceRef("container", enumerated_by=DECK_URI)
TipSite = ResourceRef("tip_site", enumerated_by=DECK_URI)
Labware = ResourceRef("labware", enumerated_by=DECK_URI)
Site = ResourceRef("site", enumerated_by=DECK_URI)
Lid = ResourceRef("lid", enumerated_by=DECK_URI)
"""Typed reference parameter types (SPEC §7.2).

Until v0.3 the bridge published an invented address pattern here, which was
finding F1: a grammar satisfiable by invention and private to this bridge.
The `resource_ref` keyword replaces it. There is no pattern, so there is
nothing to invent against, and the server validates each value against a
fresh read of the deck before a handler ever runs.
"""


def map_error(exc: BaseException) -> LabwireError:
    """Translate a PyLabRobot exception into the Labwire taxonomy.

    PyLabRobot has no common base exception, so this is an explicit table
    with a conservative default: anything unrecognized is a hardware fault
    rather than something an agent might be tempted to retry.

    Example:
        >>> type(map_error(RuntimeError("boom"))).__name__
        'HardwareFaultError'
    """
    if isinstance(exc, LabwireError):
        return exc
    name = type(exc).__name__

    if name == "ChannelizedError":
        # Per-channel failures keyed by channel index. Collapsing them to one
        # message would throw away which channel failed, so the first is
        # mapped for its category and the whole map is reported.
        errors = cast("dict[int, BaseException]", getattr(exc, "errors", {}) or {})
        detail = {str(index): f"{type(e).__name__}: {e}" for index, e in errors.items()}
        first = next(iter(errors.values()), None)
        mapped = map_error(first) if first is not None else HardwareFaultError(str(exc))
        return type(mapped)(
            f"{len(errors)} channel(s) failed: {mapped}",
            details={"channels": detail},
        )

    if name in {"NoTipError", "HasTipError"}:
        # A physical precondition is wrong; the fix is an operation, not a retry.
        return InterlockError(str(exc) or name)
    if name in {
        "TooLittleLiquidError",
        "TooLittleVolumeError",
        "ResourceNotFoundError",
        "NoChannelError",
        "ChannelsDoNotFitError",
        "NoLocationError",
    }:
        return ValidationError(str(exc) or name)
    return HardwareFaultError(f"{name}: {exc}" if str(exc) else name)


class TipResult(BaseModel):
    """What a tip operation did.

    Typed rather than a bare mapping because an opaque result cannot carry
    unit codes, and a protocol that cannot say what it returned is not much
    of a protocol. See SPEC-FINDINGS.md, finding F5.

    Example:
        >>> TipResult(channels_used=[0]).channels_used
        [0]
    """

    model_config = ConfigDict(extra="forbid")

    tip_spots: list[str] = []
    channels_used: list[int] = []
    returned: bool = False
    discarded: bool = False


class LiquidResult(BaseModel):
    """What an aspirate or dispense moved.

    Example:
        >>> LiquidResult(wells=["w"], total_volume_ul=50.0).total_volume_ul
        50.0
    """

    model_config = ConfigDict(extra="forbid")

    wells: list[str]
    total_volume_ul: float


class TransferResult(BaseModel):
    """What a transfer moved, and where.

    Example:
        >>> TransferResult(source="s", targets=["t"], total_volume_ul=10.0).total_volume_ul
        10.0
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    targets: list[str]
    total_volume_ul: float


class WellVolumeResult(BaseModel):
    """The volume a well is now recorded as holding.

    Example:
        >>> WellVolumeResult(well="w", volume_ul=200.0).volume_ul
        200.0
    """

    model_config = ConfigDict(extra="forbid")

    well: str
    volume_ul: float


class MoveResult(BaseModel):
    """What a gripper move did: the thing, where it was, where it is now.

    Example:
        >>> MoveResult(moved="labwire:deck/p", origin="labwire:deck/a", to="labwire:deck/b").to
        'labwire:deck/b'
    """

    model_config = ConfigDict(extra="forbid")

    moved: str
    origin: str
    to: str


class StopResult(BaseModel):
    """Confirmation that the handler was stopped.

    Example:
        >>> StopResult(stopped=True).stopped
        True
    """

    model_config = ConfigDict(extra="forbid")

    stopped: bool


class PyLabRobotBridge(Instrument):
    """A PyLabRobot liquid handler exposed as a Labwire instrument.

    Built by :func:`PyLabRobotInstrument`, which supplies the identity and
    the annotation file. Construct it through that factory rather than
    directly, so the annotations are checked against the deck first.

    Example:
        >>> # instrument = PyLabRobotInstrument(lh)
    """

    max_concurrent_commands = 1
    """A liquid handler has one arm; overlapping commands would be fiction."""

    deck = resource(
        DECK_URI,
        kind="deck",
        title="Deck",
        description=(
            "What is on the deck right now: the labware standing on it, what each "
            "pipetting channel holds, and the volume of every container believed to "
            "hold liquid. Every container, tip site, labware and site a command "
            "parameter can name is listed in this resource's index. Changes whenever "
            "labware or liquid moves."
        ),
        content_model=DeckState,
        item_kinds=[
            "labware",
            "plate",
            "tip_rack",
            "trough",
            "trash",
            "lid",
            "container",
            "tip_site",
            "site",
        ],
    )

    tips_mounted = channel(
        "tips_mounted",
        unit="1",
        dtype="int64",
        description="How many pipetting channels currently hold a tip.",
    )
    volume_aspirated_ul = channel(
        "volume_aspirated_ul",
        unit="uL",
        description="Total volume drawn out of containers since the server started.",
        qudt_quantity_kind="Volume",
    )
    volume_dispensed_ul = channel(
        "volume_dispensed_ul",
        unit="uL",
        description="Total volume pushed into containers since the server started.",
        qudt_quantity_kind="Volume",
    )

    def __init__(self, liquid_handler: Any, annotations: AnnotationFile) -> None:
        super().__init__()
        self._lh = liquid_handler
        self._annotations = annotations
        self._aspirated = 0.0
        self._dispensed = 0.0

    # --- plumbing ----------------------------------------------------------

    @deck.reader
    def _read_deck(self) -> ResourceSnapshot:
        return deck_snapshot(self._lh, self._annotations)

    def _publish_state(self) -> None:
        mounted = sum(
            1 for tracker in (getattr(self._lh, "head", None) or {}).values() if tracker.has_tip
        )
        self.tips_mounted.publish(mounted)
        self.volume_aspirated_ul.publish(self._aspirated)
        self.volume_dispensed_ul.publish(self._dispensed)
        self.deck.touch()

    def _refuse_locked(self, resources: list[Any]) -> None:
        """Refuse an operation that touches locked labware.

        This is the only safety escalation Labwire v0.2 can actually enforce:
        the protocol's confirmation gate cannot distinguish S2 from S3, so a
        hazard annotation has no enforcement to attach to. A hard refusal
        does. See SPEC-FINDINGS.md.
        """
        locked = locked_labware(self._annotations, resources)
        if locked:
            raise InterlockError(
                f"{', '.join(locked)} is locked by the annotation file and cannot be "
                "operated on; unlock it in the annotation file to allow this"
            )

    async def _operate(self, ctx: CommandContext, coro: Any, what: str) -> None:
        """Await a PyLabRobot operation to completion, mapping its errors.

        There is deliberately no cancellation here. PyLabRobot has no abort:
        a Hamilton STAR command is on the USB wire before any cancel can
        matter, and the Flex's stop request returning does not mean motion
        stopped (SPEC-FINDINGS F10, field-reported). The old behavior,
        abandoning this await and reporting canceled while hardware kept
        moving, was the exact failure the field report indicted. Every
        atomic command therefore declares cancel_semantics "none" and this
        await runs to the end; the one sequenced command (transfer) stops
        only at step boundaries, never inside a call.
        """
        del ctx  # cancellation is settled at boundaries, never mid-call
        try:
            await cast("Awaitable[Any]", coro)
        except Exception as exc:
            raise map_error(exc) from exc

    def _check_lengths(self, addresses: list[str], volumes: list[float], what: str) -> None:
        if len(addresses) != len(volumes):
            raise ValidationError(
                f"{what} was given {len(addresses)} address(es) and {len(volumes)} volume(s); "
                "they must correspond one to one"
            )
        if not addresses:
            raise ValidationError(f"{what} needs at least one address")

    # --- operations --------------------------------------------------------

    async def do_pick_up_tips(
        self,
        ctx: CommandContext,
        tip_spots: list[TipSite],  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
        channels: list[int] | None = None,
    ) -> TipResult:
        """Mount tips from the named spots."""
        spots = resolve_all(self._lh, tip_spots)
        self._refuse_locked(spots)
        await self._operate(
            ctx, self._lh.pick_up_tips(spots, use_channels=channels), "pick_up_tips"
        )
        self._publish_state()
        return TipResult(tip_spots=tip_spots, channels_used=channels or list(range(len(spots))))

    async def do_drop_tips(
        self,
        ctx: CommandContext,
        tip_spots: list[TipSite],  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
        channels: list[int] | None = None,
    ) -> TipResult:
        """Drop the mounted tips at the named spots."""
        spots = resolve_all(self._lh, tip_spots)
        self._refuse_locked(spots)
        await self._operate(ctx, self._lh.drop_tips(spots, use_channels=channels), "drop_tips")
        self._publish_state()
        return TipResult(tip_spots=tip_spots)

    async def do_return_tips(self, ctx: CommandContext) -> TipResult:
        """Return the mounted tips to where they came from."""
        await self._operate(ctx, self._lh.return_tips(), "return_tips")
        self._publish_state()
        return TipResult(returned=True)

    async def do_discard_tips(self, ctx: CommandContext) -> TipResult:
        """Discard the mounted tips into the trash."""
        await self._operate(ctx, self._lh.discard_tips(), "discard_tips")
        self._publish_state()
        return TipResult(discarded=True)

    async def do_aspirate(
        self,
        ctx: CommandContext,
        wells: list[Container],  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
        volumes_ul: list[float],
        flow_rates_ul_s: list[float] | None = None,
    ) -> LiquidResult:
        """Draw liquid out of the named containers."""
        self._check_lengths(wells, volumes_ul, "aspirate")
        containers = resolve_all(self._lh, wells)
        self._refuse_locked(containers)
        await self._operate(
            ctx,
            self._lh.aspirate(containers, vols=volumes_ul, flow_rates=flow_rates_ul_s),
            "aspirate",
        )
        self._aspirated += sum(volumes_ul)
        self._publish_state()
        return LiquidResult(wells=wells, total_volume_ul=sum(volumes_ul))

    async def do_dispense(
        self,
        ctx: CommandContext,
        wells: list[Container],  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
        volumes_ul: list[float],
        flow_rates_ul_s: list[float] | None = None,
    ) -> LiquidResult:
        """Push liquid into the named containers."""
        self._check_lengths(wells, volumes_ul, "dispense")
        containers = resolve_all(self._lh, wells)
        self._refuse_locked(containers)
        await self._operate(
            ctx,
            self._lh.dispense(containers, vols=volumes_ul, flow_rates=flow_rates_ul_s),
            "dispense",
        )
        self._dispensed += sum(volumes_ul)
        self._publish_state()
        return LiquidResult(wells=wells, total_volume_ul=sum(volumes_ul))

    async def do_transfer(
        self,
        ctx: CommandContext,
        source: Container,  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
        targets: list[Container],  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
        volumes_ul: list[float],
    ) -> TransferResult:
        """Move liquid from one well into others, stopping only at boundaries.

        The bridge sequences this itself (one aspirate, then one dispense
        per target), which is what earns it cancel_semantics
        "between_steps": a cancel finishes the PLR call in flight and stops
        before the next one is issued, and the record names the boundary.
        A cancel after the aspirate leaves liquid in the tip. The deck
        resource shows the source's deficit and the mounted tip, because
        each step's accounting lands when the step does; the tip's own
        contents are tracked by PyLabRobot but not exposed per channel.
        """
        self._check_lengths(targets, volumes_ul, "transfer")
        source_well = resolve(self._lh, source)
        target_wells = resolve_all(self._lh, targets)
        self._refuse_locked([source_well, *target_wells])
        total = sum(volumes_ul)
        steps = 1 + len(target_wells)

        await self._operate(ctx, self._lh.aspirate([source_well], vols=[total]), "aspirate")
        self._aspirated += total
        self._publish_state()
        ctx.boundary("aspirate", of=steps)

        for index, (well, volume, uri) in enumerate(
            zip(target_wells, volumes_ul, targets, strict=True)
        ):
            await self._operate(ctx, self._lh.dispense([well], vols=[volume]), f"dispense {uri}")
            self._dispensed += volume
            self._publish_state()
            if index < len(target_wells) - 1:  # no boundary after the last step
                ctx.boundary(f"dispense {uri}", of=steps)
        return TransferResult(source=source, targets=targets, total_volume_ul=total)

    async def do_set_well_volume(
        self,
        ctx: CommandContext,
        well: Container,  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
        volume_ul: float,
    ) -> WellVolumeResult:
        """Declare how much liquid a well already holds."""
        container = resolve(self._lh, well)
        self._refuse_locked([container])
        tracker = getattr(container, "tracker", None)
        if tracker is None:
            raise ValidationError(f"{well!r} is not a container and holds no volume")
        maximum = getattr(container, "max_volume", None)
        if isinstance(maximum, int | float) and volume_ul > float(maximum):
            raise ValidationError(
                f"{well!r} holds at most {float(maximum)} uL; {volume_ul} uL would overfill it"
            )
        try:
            tracker.set_volume(volume_ul)
        except Exception as exc:
            raise map_error(exc) from exc
        return WellVolumeResult(well=well, volume_ul=volume_ul)

    async def _move_gripped(
        self, ctx: CommandContext, moved_uri: str, to_uri: str, op: str
    ) -> MoveResult:
        thing = resolve(self._lh, moved_uri)
        destination = resolve(self._lh, to_uri)
        self._refuse_locked([thing, destination])
        parent = getattr(thing, "parent", None)
        # Standing directly on the deck, the origin is the deck resource
        # itself, not "labwire:deck/<deck's own name>".
        if parent is None or parent is self._lh.deck:
            origin = DECK_URI
        else:
            origin = f"{DECK_URI}/{parent.name}"
        operation = getattr(self._lh, op)
        await self._operate(ctx, operation(thing, destination), op)
        self._publish_state()
        return MoveResult(moved=moved_uri, origin=origin, to=to_uri)

    async def do_move_plate(
        self,
        ctx: CommandContext,
        plate: Labware,  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
        to: Site,  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
    ) -> MoveResult:
        """Grip a plate and set it down on another site."""
        return await self._move_gripped(ctx, plate, to, "move_plate")

    async def do_move_lid(
        self,
        ctx: CommandContext,
        lid: Lid,  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
        to: Labware,  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
    ) -> MoveResult:
        """Grip a plate lid and move it onto another plate."""
        return await self._move_gripped(ctx, lid, to, "move_lid")

    async def do_move_resource(
        self,
        ctx: CommandContext,
        moved: Labware,  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
        to: Site,  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType, reportGeneralTypeIssues]
    ) -> MoveResult:
        """Grip any labware and move it to a site."""
        return await self._move_gripped(ctx, moved, to, "move_resource")

    async def do_stop(self, ctx: CommandContext) -> StopResult:
        """Stop the liquid handler."""
        try:
            await self._lh.stop()
        except Exception as exc:
            raise map_error(exc) from exc
        return StopResult(stopped=True)


_IMPLEMENTATIONS: dict[str, str] = {
    "pick_up_tips": "do_pick_up_tips",
    "drop_tips": "do_drop_tips",
    "return_tips": "do_return_tips",
    "discard_tips": "do_discard_tips",
    "aspirate": "do_aspirate",
    "dispense": "do_dispense",
    "transfer": "do_transfer",
    "set_well_volume": "do_set_well_volume",
    "move_plate": "do_move_plate",
    "move_lid": "do_move_lid",
    "move_resource": "do_move_resource",
    "stop": "do_stop",
}

_UNITS: dict[str, dict[str, str]] = {
    # A channel index is a count, so it is dimensionless rather than unitless:
    # "1" is the UCUM code that says so out loud.
    "pick_up_tips": {"channels": "1"},
    "drop_tips": {"channels": "1"},
    "aspirate": {"volumes_ul": "uL", "flow_rates_ul_s": "uL/s"},
    "dispense": {"volumes_ul": "uL", "flow_rates_ul_s": "uL/s"},
    "transfer": {"volumes_ul": "uL"},
    "set_well_volume": {"volume_ul": "uL"},
}

_RETURNS_UNITS: dict[str, dict[str, str]] = {
    "pick_up_tips": {"channels_used[]": "1"},
    "drop_tips": {"channels_used[]": "1"},
    "return_tips": {"channels_used[]": "1"},
    "discard_tips": {"channels_used[]": "1"},
    "aspirate": {"total_volume_ul": "uL"},
    "dispense": {"total_volume_ul": "uL"},
    "transfer": {"total_volume_ul": "uL"},
    "set_well_volume": {"volume_ul": "uL"},
}


def _fresh(implementation: Any) -> Any:
    """A decoratable copy of a base-class method.

    ``@command`` records its metadata *on the function object*. Applying it
    straight to ``PyLabRobotBridge.do_transfer`` would brand the shared base
    method, so an annotation that excluded ``transfer`` for one instrument
    would still leave it exposed through the base class, and the exclusion
    would leak into every instrument built afterwards. Copying first keeps
    each generated instrument's command surface its own.

    ``functools.update_wrapper`` carries the signature, annotations, and name
    across, so the decorator sees exactly what it would have seen.

    Example:
        >>> _fresh(PyLabRobotBridge.do_stop).__name__
        'do_stop'
    """

    async def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        return await implementation(self, *args, **kwargs)

    functools.update_wrapper(call, implementation)
    return call


def PyLabRobotInstrument(
    liquid_handler: Any,
    annotations: AnnotationFile | None = None,
) -> PyLabRobotBridge:
    """Build a Labwire instrument backed by a live PyLabRobot liquid handler.

    The annotation file is checked against the deck first, so a hazard
    annotation naming a plate that is not loaded is a refusal rather than a
    silent no-op. Tip and volume tracking are enabled process-wide.

    Example:
        >>> # instrument = PyLabRobotInstrument(lh, load_annotations(path))
    """
    annotations = annotations or AnnotationFile()
    draft = introspect(liquid_handler)
    surface = command_surface()

    check(
        annotations,
        known_resources={item.uri for item in draft.labware},
        known_labware={item.type_name for item in draft.labware}
        | {item.model for item in draft.labware if item.model},
        known_commands={spec.name for spec in surface},
    )

    _enable_tracking()

    description = (
        annotations.instrument.description
        or f"{draft.backend_class} exposed through the Labwire PyLabRobot bridge."
    )
    namespace: dict[str, Any] = {
        "identity": draft.identity,
        "intent_tags": annotations.instrument.intent_tags or ["liquid_handling"],
        "__doc__": description,
    }

    for spec in surface:
        override = annotations.commands.get(spec.name)
        if override is not None and override.exclude:
            continue
        implementation = _fresh(getattr(PyLabRobotBridge, _IMPLEMENTATIONS[spec.name]))
        namespace[spec.name] = command(
            name=spec.name,
            units=_UNITS.get(spec.name, {}),
            returns_units=_RETURNS_UNITS.get(spec.name, {}),
            safety_class=cast(
                "Any", (override.safety_class if override else None) or spec.safety_class
            ),
            description=(override.description if override else None) or spec.description,
            estimated_duration_s=(override.estimated_duration_s if override else None),
            # Every atomic PLR call is committed once issued (F10): a plate
            # held in the gripper mid-traverse has no safe interruption, and
            # an aspirate is on the wire before any cancel can matter. The
            # one exception is transfer, which this bridge sequences itself
            # and can therefore honestly stop between steps.
            cancel="between_steps" if spec.name == "transfer" else "none",
        )(implementation)

    generated = type("PyLabRobot_LiquidHandler", (PyLabRobotBridge,), namespace)
    return generated(liquid_handler, annotations)


def _enable_tracking() -> None:
    """Turn on PyLabRobot's tip and volume trackers.

    Both default to off and are toggled by module-level globals, so this
    affects every liquid handler in the process. Serving without them would
    mean serving an instrument that silently accepts commands it cannot
    physically perform.
    """
    try:
        from pylabrobot.resources import set_tip_tracking, set_volume_tracking
    except ImportError:  # pragma: no cover - the caller already has a handler
        return
    set_tip_tracking(True)
    set_volume_tracking(True)
