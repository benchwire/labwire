"""EXPERIMENTAL: a plain-class PyLabRobot device as a Labwire instrument.

Branch ``plr-v1`` only; nothing here is published. PyLabRobot's v1b1
redesign replaces the LiquidHandler/backend split with one plain Python
class per device (no base class, no registry, nothing to introspect).
This module tests whether Labwire's bridge machinery survives that
world, using the first plain-class liquid handler in existence:
vcjdeboer's Opentrons Flex driver from PyLabRobot PR #1184.

Pinned upstream: the PR head commit, because the driver exists only in
the PR (its v1b1 base was force-pushed from under it on 2026-08-01).
Refresh the pin deliberately, never automatically; V1B1.md records what
was true at this commit.

The mapping under test, stated once:

- **One plain-class device instance is one Labwire instrument.** The
  device alone is not enough: the resource machinery needs a deck root
  and a channel-state reader, which the plain-class model does not
  standardize, so :class:`DeckBoundDevice` binds ``(device, deck)``
  explicitly and presents the small surface the shipped bridge modules
  already consume. ``introspect``, ``deck``, and ``addressing`` are
  reused UNCHANGED; that reuse is the experiment's central result.
- Commands carry the same UCUM units, safety classes, and cancel
  semantics as the shipped bridge, from the same tables. Every atomic
  Flex call is committed once issued (its HTTP command is on the wire;
  SPEC-FINDINGS F10 came from a Flex owner), so everything declares
  ``cancel_semantics: "none"`` except bridge-sequenced ``transfer``.
- Channel state (which channel holds a tip) is read from the driver's
  private ``_channel_tips`` list because the plain-class model has no
  public reader for it. That is a documented strain, not a footnote.

Like the rest of this package, the module imports cleanly without
PyLabRobot installed; the PR-head install is needed only to run a real
``OpentronsFlex``.

Example:
    >>> # flex = OpentronsFlex(deck=FlexDeck(), host="169.254.99.87")
    >>> # await flex.setup()
    >>> # instrument = OpentronsFlexInstrument(flex)
"""

from typing import Any, cast

from labwire.bridges.pylabrobot.annotations import AnnotationFile, check
from labwire.bridges.pylabrobot.bridge import (
    _IMPLEMENTATIONS,  # pyright: ignore[reportPrivateUsage]
    _RETURNS_UNITS,  # pyright: ignore[reportPrivateUsage]
    _UNITS,  # pyright: ignore[reportPrivateUsage]
    PyLabRobotBridge,
    _enable_tracking,  # pyright: ignore[reportPrivateUsage]
    _fresh,  # pyright: ignore[reportPrivateUsage]
    map_error,
)
from labwire.bridges.pylabrobot.introspect import (
    DraftCommand,
    IdentityInfo,
    _pylabrobot_version,  # pyright: ignore[reportPrivateUsage]
    introspect,
)
from labwire.core import CommandContext, ValidationError, command
from pydantic import BaseModel, ConfigDict

PINNED_PLR_REPO = "https://github.com/vcjdeboer/pylabrobot"
PINNED_PLR_COMMIT = "6ee378e5af672c92b59d53f2a0e33d9b68783613"
"""PR #1184 head. The only commit this module has been exercised against."""


class _ChannelView:
    """A tracker-shaped reader over one Flex channel's mounted tip."""

    def __init__(self, tip: Any) -> None:
        self._tip = tip

    @property
    def has_tip(self) -> bool:
        return self._tip is not None

    def get_tip(self) -> Any:
        return self._tip


class DeckBoundDevice:
    """The one-instrument unit of the plain-class world: device plus deck.

    Presents exactly the surface the shipped bridge modules consume:
    resource lookup and traversal (delegated to the deck, which is still a
    first-class PLR ``Deck``), a ``head``-shaped channel-state mapping
    (synthesized from the driver's private ``_channel_tips``, the only
    channel-state reader the plain-class model offers), and the operation
    methods (delegated to the device). ``backend`` is deliberately absent:
    plain-class devices have none, and ``introspect`` already reports an
    unknown backend honestly.

    Example:
        >>> # view = DeckBoundDevice(flex, flex.deck, name="flex-1")
    """

    def __init__(self, device: Any, deck: Any, name: str) -> None:
        self.device = device
        self.deck = deck
        self.name = name

    # --- resource surface (consumed by introspect/addressing/deck) --------

    def get_resource(self, name: str) -> Any:
        """Look up a resource by name anywhere under the deck."""
        return self.deck.get_resource(name)

    def get_all_children(self) -> list[Any]:
        """Every resource under the deck, recursively."""
        return cast("list[Any]", self.deck.get_all_children())

    @property
    def head(self) -> dict[int, _ChannelView]:
        """Channel index to tracker-shaped view of the mounted tip."""
        tips = getattr(self.device, "_channel_tips", None) or []
        return {index: _ChannelView(tip) for index, tip in enumerate(tips)}

    # --- operations (consumed by PyLabRobotBridge handlers) ----------------

    async def pick_up_tips(self, tip_spots: list[Any], use_channels: Any = None) -> None:
        """Forward to the device."""
        await self.device.pick_up_tips(tip_spots, use_channels=use_channels)

    async def drop_tips(self, tip_spots: list[Any], use_channels: Any = None) -> None:
        """Forward to the device."""
        await self.device.drop_tips(tip_spots, use_channels=use_channels)

    async def aspirate(self, resources: list[Any], vols: Any, flow_rates: Any = None) -> None:
        """Forward to the device."""
        await self.device.aspirate(resources, vols=vols, flow_rates=flow_rates)

    async def dispense(self, resources: list[Any], vols: Any, flow_rates: Any = None) -> None:
        """Forward to the device."""
        await self.device.dispense(resources, vols=vols, flow_rates=flow_rates)

    async def stop(self) -> None:
        """Forward to the device."""
        await self.device.stop()


class HomeResult(BaseModel):
    """Confirmation that the gantry homed.

    Example:
        >>> HomeResult(homed=True).homed
        True
    """

    model_config = ConfigDict(extra="forbid")

    homed: bool


class DiscardResult(BaseModel):
    """Which channels dropped their tips into the trash.

    Example:
        >>> DiscardResult(channels_used=[0]).channels_used
        [0]
    """

    model_config = ConfigDict(extra="forbid")

    channels_used: list[int]


class OpentronsFlexBridge(PyLabRobotBridge):
    """The shipped bridge handlers, plus the two Flex-specific commands.

    Built by :func:`OpentronsFlexInstrument`; ``self._lh`` is a
    :class:`DeckBoundDevice`, and every inherited handler works through it
    unchanged.

    Example:
        >>> # instrument = OpentronsFlexInstrument(flex)
    """

    async def do_home(self, ctx: CommandContext) -> HomeResult:
        """Home all axes; the gantry moves to the rear-left-top."""
        del ctx  # committed once issued, like every atomic call here
        view = cast("DeckBoundDevice", self._lh)
        try:
            await view.device.home()
        except Exception as exc:
            raise map_error(exc) from exc
        self._publish_state()
        return HomeResult(homed=True)

    async def do_discard_tips(self, ctx: CommandContext) -> DiscardResult:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Drop every mounted tip into the deck's trash.

        The Flex driver has no discard operation; this sequences one
        drop-to-trash per mounted channel, which is why it is the second
        command here (after transfer) honest enough to declare
        ``between_steps``.
        """
        view = cast("DeckBoundDevice", self._lh)
        trash = view.deck.get_trash_area()
        mounted = [index for index, channel in view.head.items() if channel.has_tip]
        if not mounted:
            raise ValidationError("no tips are mounted; nothing to discard")
        try:
            for position, index in enumerate(mounted):
                await view.device.drop_tips([trash], use_channels=[index])
                self._publish_state()
                if position < len(mounted) - 1:
                    ctx.boundary(f"discarded channel {index}", of=len(mounted))
        except Exception as exc:
            raise map_error(exc) from exc
        return DiscardResult(channels_used=mounted)


def flex_command_surface() -> list[DraftCommand]:
    """The commands the experimental Flex bridge exposes.

    A curated table, exactly like the shipped bridge's: the plain-class
    model offers nothing to reflect over, so the typing, safety classes,
    and cancel semantics are the bridge's contribution. The subset follows
    the driver: no gripper moves, no return_tips (the driver does not
    remember origins across calls; the resource tree does, but claiming a
    return on top of it would exceed what the driver has been tested for).

    Example:
        >>> sorted(c.name for c in flex_command_surface())[:2]
        ['aspirate', 'discard_tips']
    """
    return [
        DraftCommand(
            name="pick_up_tips",
            description="Pick up tips from the given tip spots onto the pipetting channels.",
            safety_class="S2",
        ),
        DraftCommand(
            name="drop_tips",
            description="Drop the mounted tips at the given tip spots.",
            safety_class="S2",
        ),
        DraftCommand(
            name="discard_tips",
            description="Discard the mounted tips into the trash, one channel at a time.",
            safety_class="S2",
        ),
        DraftCommand(
            name="aspirate",
            description="Draw liquid out of the given containers into the mounted tips.",
            safety_class="S2",
        ),
        DraftCommand(
            name="dispense",
            description="Push liquid from the mounted tips into the given containers.",
            safety_class="S2",
        ),
        DraftCommand(
            name="transfer",
            description=(
                "Move liquid from one source well into one or more target wells, "
                "aspirating and dispensing in one command."
            ),
            safety_class="S2",
        ),
        DraftCommand(
            name="set_well_volume",
            description=(
                "Declare how much liquid a well already contains. This moves nothing; it "
                "corrects the instrument's own record, which cannot see into a plate a "
                "human placed on the deck."
            ),
            safety_class="S1",
        ),
        DraftCommand(
            name="home",
            description=(
                "Home all axes. The gantry moves to the rear-left-top, travelling over "
                "whatever is on the deck."
            ),
            safety_class="S1",
        ),
        DraftCommand(
            name="stop",
            description=(
                "Stop the robot's current run. Acknowledgment is not settlement: on a "
                "Flex, the stop request returning does not mean motion has stopped."
            ),
            safety_class="S0",
        ),
    ]


_FLEX_IMPLEMENTATIONS = dict(_IMPLEMENTATIONS) | {
    "home": "do_home",
    "discard_tips": "do_discard_tips",
}

_FLEX_RETURNS_UNITS = dict(_RETURNS_UNITS) | {"discard_tips": {"channels_used[]": "1"}}


def OpentronsFlexInstrument(
    device: Any,
    deck: Any | None = None,
    annotations: AnnotationFile | None = None,
    name: str = "opentrons-flex",
) -> OpentronsFlexBridge:
    """EXPERIMENTAL: build a Labwire instrument from a plain-class Flex.

    ``deck`` defaults to ``device.deck`` because the Flex driver happens to
    carry one; the parameter exists because nothing in the plain-class
    model says the next device will. The annotation file format is the
    shipped bridge's, unchanged.

    Example:
        >>> # instrument = OpentronsFlexInstrument(flex)
    """
    deck = deck if deck is not None else getattr(device, "deck", None)
    if deck is None:
        raise ValueError(
            "the device carries no deck; pass one explicitly "
            "(plain-class devices are not required to hold their deck)"
        )
    annotations = annotations or AnnotationFile()
    view = DeckBoundDevice(device, deck, name=name)
    draft = introspect(view)
    surface = flex_command_surface()

    check(
        annotations,
        known_resources={item.uri for item in draft.labware},
        known_labware={item.type_name for item in draft.labware}
        | {item.model for item in draft.labware if item.model},
        known_commands={spec.name for spec in surface},
    )

    _enable_tracking()

    robot_model = getattr(device, "robot_model", None) or "Flex (not yet connected)"
    api_version = getattr(device, "api_version", None) or "unknown"
    identity = IdentityInfo(
        manufacturer="Opentrons (via PyLabRobot PR #1184; never tested on hardware)",
        model=str(robot_model),
        serial_number=name,
        firmware_version=f"pylabrobot {_pylabrobot_version()}; robot API {api_version}",
    )

    description = annotations.instrument.description or (
        "EXPERIMENTAL plain-class bridge: an Opentrons Flex (PyLabRobot PR #1184, "
        f"pinned at {PINNED_PLR_COMMIT[:12]}) exposed through the Labwire protocol. "
        "Exercised against a simulation of the robot-server command layer only."
    )
    namespace: dict[str, Any] = {
        "identity": identity,
        "intent_tags": annotations.instrument.intent_tags or ["liquid_handling"],
        "__doc__": description,
    }

    for spec in surface:
        override = annotations.commands.get(spec.name)
        if override is not None and override.exclude:
            continue
        implementation = _fresh(getattr(OpentronsFlexBridge, _FLEX_IMPLEMENTATIONS[spec.name]))
        namespace[spec.name] = command(
            name=spec.name,
            units=_UNITS.get(spec.name, {}),
            returns_units=_FLEX_RETURNS_UNITS.get(spec.name, {}),
            safety_class=cast(
                "Any", (override.safety_class if override else None) or spec.safety_class
            ),
            description=(override.description if override else None) or spec.description,
            estimated_duration_s=(override.estimated_duration_s if override else None),
            # Same discipline as the shipped bridge, same F10 provenance
            # (the field report was from a Flex owner): an HTTP command is
            # on the wire once issued, so atomic calls are "none"; only
            # the two bridge-sequenced commands can honestly stop between
            # steps.
            cancel="between_steps" if spec.name in {"transfer", "discard_tips"} else "none",
        )(implementation)

    generated = type("OpentronsFlex_PlainClass", (OpentronsFlexBridge,), namespace)
    return generated(view, annotations)
