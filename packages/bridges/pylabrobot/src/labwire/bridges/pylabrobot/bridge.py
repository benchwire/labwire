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

import asyncio
import contextlib
import functools
from collections.abc import Awaitable
from typing import Annotated, Any, cast

from labwire.bridges.pylabrobot.addressing import ADDRESS_PATTERN, resolve, resolve_all
from labwire.bridges.pylabrobot.annotations import AnnotationFile, check
from labwire.bridges.pylabrobot.deck import deck_state, locked_labware
from labwire.bridges.pylabrobot.introspect import command_surface, introspect
from labwire.core import (
    CanceledError,
    CommandContext,
    HardwareFaultError,
    Instrument,
    InterlockError,
    LabwireError,
    ValidationError,
    channel,
    command,
)
from pydantic import StringConstraints

_POLL_S = 0.02

Location = Annotated[str, StringConstraints(pattern=ADDRESS_PATTERN)]
"""An address parameter, shape-checked by JSON Schema before it is resolved.

The pattern is all JSON Schema can say. That a well exists on *this* deck is
not expressible, so it is checked at resolution time and reported as a
validation error naming what would have worked. See SPEC-FINDINGS.md.
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

    def _publish_state(self) -> None:
        mounted = sum(
            1 for tracker in (getattr(self._lh, "head", None) or {}).values() if tracker.has_tip
        )
        self.tips_mounted.publish(mounted)
        self.volume_aspirated_ul.publish(self._aspirated)
        self.volume_dispensed_ul.publish(self._dispensed)

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
        """Await a PyLabRobot operation, honouring cancellation as far as it can.

        Each operation is a single await, so cancellation means stopping the
        handler and abandoning the call rather than interrupting it partway.
        Against the chatterbox backend operations complete immediately, so a
        cancel almost always loses the race. What that means on hardware has
        never been tested. See LIMITATIONS.
        """
        task = asyncio.ensure_future(coro)
        try:
            while not task.done():
                if ctx.cancel_requested:
                    stop = getattr(self._lh, "stop", None)
                    if callable(stop):
                        # A failed stop must not mask the cancellation itself.
                        with contextlib.suppress(Exception):
                            await cast("Awaitable[None]", stop())
                    task.cancel()
                    raise CanceledError(f"{what} canceled by request")
                await ctx.sleep(_POLL_S)
        finally:
            if not task.done():
                task.cancel()
        exception = task.exception()
        if exception is not None:
            raise map_error(exception)

    def _check_lengths(self, addresses: list[str], volumes: list[float], what: str) -> None:
        if len(addresses) != len(volumes):
            raise ValidationError(
                f"{what} was given {len(addresses)} address(es) and {len(volumes)} volume(s); "
                "they must correspond one to one"
            )
        if not addresses:
            raise ValidationError(f"{what} needs at least one address")

    # --- operations --------------------------------------------------------

    async def do_describe_deck(self, ctx: CommandContext) -> dict[str, Any]:
        """Project the deck. Pure read, no motion."""
        return deck_state(self._lh, self._annotations).model_dump(mode="json")

    async def do_pick_up_tips(
        self,
        ctx: CommandContext,
        tip_spots: list[Location],
        channels: list[int] | None = None,
    ) -> dict[str, Any]:
        """Mount tips from the named spots."""
        spots = resolve_all(self._lh, tip_spots)
        self._refuse_locked(spots)
        await self._operate(
            ctx, self._lh.pick_up_tips(spots, use_channels=channels), "pick_up_tips"
        )
        self._publish_state()
        return {"tip_spots": tip_spots, "channels_used": channels or list(range(len(spots)))}

    async def do_drop_tips(
        self,
        ctx: CommandContext,
        tip_spots: list[Location],
        channels: list[int] | None = None,
    ) -> dict[str, Any]:
        """Drop the mounted tips at the named spots."""
        spots = resolve_all(self._lh, tip_spots)
        self._refuse_locked(spots)
        await self._operate(ctx, self._lh.drop_tips(spots, use_channels=channels), "drop_tips")
        self._publish_state()
        return {"tip_spots": tip_spots}

    async def do_return_tips(self, ctx: CommandContext) -> dict[str, Any]:
        """Return the mounted tips to where they came from."""
        await self._operate(ctx, self._lh.return_tips(), "return_tips")
        self._publish_state()
        return {"returned": True}

    async def do_discard_tips(self, ctx: CommandContext) -> dict[str, Any]:
        """Discard the mounted tips into the trash."""
        await self._operate(ctx, self._lh.discard_tips(), "discard_tips")
        self._publish_state()
        return {"discarded": True}

    async def do_aspirate(
        self,
        ctx: CommandContext,
        wells: list[Location],
        volumes_ul: list[float],
        flow_rates_ul_s: list[float] | None = None,
    ) -> dict[str, Any]:
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
        return {"wells": wells, "total_volume_ul": sum(volumes_ul)}

    async def do_dispense(
        self,
        ctx: CommandContext,
        wells: list[Location],
        volumes_ul: list[float],
        flow_rates_ul_s: list[float] | None = None,
    ) -> dict[str, Any]:
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
        return {"wells": wells, "total_volume_ul": sum(volumes_ul)}

    async def do_transfer(
        self,
        ctx: CommandContext,
        source: Location,
        targets: list[Location],
        volumes_ul: list[float],
    ) -> dict[str, Any]:
        """Move liquid from one well into others in a single command."""
        self._check_lengths(targets, volumes_ul, "transfer")
        source_well = resolve(self._lh, source)
        target_wells = resolve_all(self._lh, targets)
        self._refuse_locked([source_well, *target_wells])
        await self._operate(
            ctx,
            self._lh.transfer(source_well, target_wells, target_vols=volumes_ul),
            "transfer",
        )
        total = sum(volumes_ul)
        self._aspirated += total
        self._dispensed += total
        self._publish_state()
        return {"source": source, "targets": targets, "total_volume_ul": total}

    async def do_set_well_volume(
        self, ctx: CommandContext, well: Location, volume_ul: float
    ) -> dict[str, Any]:
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
        return {"well": well, "volume_ul": volume_ul}

    async def do_stop(self, ctx: CommandContext) -> dict[str, bool]:
        """Stop the liquid handler."""
        try:
            await self._lh.stop()
        except Exception as exc:
            raise map_error(exc) from exc
        return {"stopped": True}


_IMPLEMENTATIONS: dict[str, str] = {
    "describe_deck": "do_describe_deck",
    "pick_up_tips": "do_pick_up_tips",
    "drop_tips": "do_drop_tips",
    "return_tips": "do_return_tips",
    "discard_tips": "do_discard_tips",
    "aspirate": "do_aspirate",
    "dispense": "do_dispense",
    "transfer": "do_transfer",
    "set_well_volume": "do_set_well_volume",
    "stop": "do_stop",
}

_UNITS: dict[str, dict[str, str]] = {
    "aspirate": {"volumes_ul": "uL", "flow_rates_ul_s": "uL/s"},
    "dispense": {"volumes_ul": "uL", "flow_rates_ul_s": "uL/s"},
    "transfer": {"volumes_ul": "uL"},
    "set_well_volume": {"volume_ul": "uL"},
}

_RETURNS_UNITS: dict[str, dict[str, str]] = {
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
        known_resources={item.address for item in draft.labware},
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
