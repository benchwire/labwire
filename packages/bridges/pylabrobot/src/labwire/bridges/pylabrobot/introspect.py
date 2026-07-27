"""Derive a draft Labwire descriptor from a configured LiquidHandler.

Pure introspection: it reads the resource tree and the trackers already held
in memory and performs no device I/O at all, so it is safe to call against a
handler that has not been set up (the channel count is then taken from the
backend rather than from the head trackers).

The interesting difference from the ophyd bridge is what introspection *is*
here. An ophyd device's structure is fixed by its class, so introspecting it
produces a descriptor. A liquid handler's structure is a deck someone loaded
this morning, so introspecting it produces two separate things: a command
surface, which is stable and belongs in the descriptor, and a deck projection,
which is state and has nowhere to live in Labwire v0.2 except a command
result. See ``DESIGN.md`` and ``SPEC-FINDINGS.md``.

Example:
    >>> # draft = introspect(lh)
    >>> # draft.identity.model
    >>> # 'LiquidHandlerChatterboxBackend'
"""

import enum
from typing import Any

from labwire.bridges.pylabrobot.addressing import address_of
from labwire.core import IdentityInfo, SafetyClass
from pydantic import BaseModel, ConfigDict

_CATEGORY_KINDS = {
    "plate": "plate",
    "tip_rack": "tip_rack",
    "trough": "trough",
    "trash": "trash",
    "carrier": "carrier",
    "plate_carrier": "carrier",
    "tip_carrier": "carrier",
    "deck": "deck",
}


class LabwareKind(enum.StrEnum):
    """What a piece of labware is, as far as the bridge can tell."""

    PLATE = "plate"
    TIP_RACK = "tip_rack"
    TROUGH = "trough"
    TRASH = "trash"
    CARRIER = "carrier"
    DECK = "deck"
    OTHER = "other"
    """Recognized as present and addressable, but of unknown purpose."""


class UnresolvedReason(enum.StrEnum):
    """Why a piece of labware is less usable than it could be."""

    UNKNOWN_KIND = "unknown_kind"
    """PyLabRobot reports no category the bridge recognizes."""
    NO_LOCATION = "no_location"
    """Assigned to no location, so nothing can be planned around where it is."""
    NO_CAPACITY = "no_capacity"
    """An itemized container whose items report no maximum volume."""


class Grid(BaseModel):
    """The item layout of an itemized resource, such as a 96-well plate.

    Example:
        >>> Grid(rows=8, columns=12).item_count
        96
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: int
    columns: int
    item_max_volume_ul: float | None = None

    @property
    def item_count(self) -> int:
        """How many items the grid holds.

        Example:
            >>> Grid(rows=8, columns=12).item_count
            96
        """
        return self.rows * self.columns


class DraftLabware(BaseModel):
    """One addressable piece of labware on the deck.

    Example:
        >>> # draft.labware[0].address
        >>> # 'source_plate'
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    address: str
    """The name this labware is addressed by (also its PyLabRobot name)."""
    kind: LabwareKind
    type_name: str
    """The PyLabRobot class, e.g. ``Plate``."""
    model: str | None = None
    """PyLabRobot's labware model identifier, e.g. ``Cor_96_wellplate_360ul_Fb``."""
    location_mm: tuple[float, float, float] | None = None
    size_mm: tuple[float, float, float] | None = None
    grid: Grid | None = None


class DraftCommand(BaseModel):
    """One command the bridge will expose.

    Example:
        >>> # {c.name: c.safety_class for c in draft.commands}
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    safety_class: SafetyClass


class Unresolved(BaseModel):
    """A gap worth reporting, named precisely.

    Example:
        >>> # [u.message for u in draft.unresolved]
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    address: str
    reason: UnresolvedReason
    message: str


class DraftInstrument(BaseModel):
    """The introspected draft: identity, deck contents, and command surface.

    Example:
        >>> # draft.channel_count
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: IdentityInfo
    backend_class: str
    channel_count: int
    labware: list[DraftLabware]
    commands: list[DraftCommand]
    unresolved: list[Unresolved]

    @property
    def is_complete(self) -> bool:
        """Whether every piece of labware introspected cleanly.

        Example:
            >>> # introspect(lh).is_complete
        """
        return not self.unresolved

    def find(self, address: str) -> DraftLabware:
        """Look up labware by address.

        Example:
            >>> # draft.find("source_plate").kind
        """
        for candidate in self.labware:
            if candidate.address == address:
                return candidate
        raise KeyError(f"no such labware: {address!r}")


def _pylabrobot_version() -> str:
    try:
        from importlib.metadata import version

        return version("pylabrobot")
    except Exception:  # pragma: no cover - PyLabRobot is a dependency of this package
        return "unknown"


def _coordinate(value: Any) -> tuple[float, float, float] | None:
    if value is None:
        return None
    try:
        return (float(value.x), float(value.y), float(value.z))
    except Exception:  # pragma: no cover - a Coordinate always has x/y/z
        return None


def _kind_of(resource: Any) -> LabwareKind:
    category = getattr(resource, "category", None)
    if isinstance(category, str):
        mapped = _CATEGORY_KINDS.get(category)
        if mapped is not None:
            return LabwareKind(mapped)
    return LabwareKind.OTHER


def _grid_of(resource: Any) -> Grid | None:
    rows = getattr(resource, "num_items_y", None)
    columns = getattr(resource, "num_items_x", None)
    if not isinstance(rows, int) or not isinstance(columns, int) or rows < 1 or columns < 1:
        return None
    capacity: float | None = None
    try:
        first = resource.get_item(0)
    except Exception:  # pragma: no cover - a sized grid always has an item 0
        first = None
    raw = getattr(first, "max_volume", None)
    if isinstance(raw, int | float):
        capacity = float(raw)
    return Grid(rows=rows, columns=columns, item_max_volume_ul=capacity)


def addressable_resources(root: Any) -> list[Any]:
    """Deck children an agent addresses directly, not their items.

    Wells and tip spots are reached through their parent, so listing them here
    would turn a two-plate deck into two hundred entries of noise.
    """
    resources: list[Any] = []
    for child in root.get_all_children():
        parent = getattr(child, "parent", None)
        if parent is not None and hasattr(parent, "get_child_identifier"):
            continue
        if _kind_of(child) is LabwareKind.DECK:
            continue
        resources.append(child)
    return resources


def _describe_labware(resource: Any) -> tuple[DraftLabware, list[Unresolved]]:
    address = address_of(resource)
    kind = _kind_of(resource)
    grid = _grid_of(resource)
    try:
        location = _coordinate(resource.get_absolute_location())
    except Exception:
        location = None

    gaps: list[Unresolved] = []
    if kind is LabwareKind.OTHER:
        gaps.append(
            Unresolved(
                address=address,
                reason=UnresolvedReason.UNKNOWN_KIND,
                message=(
                    f"{address!r} is a {type(resource).__name__} with category "
                    f"{getattr(resource, 'category', None)!r}, which the bridge does not "
                    "recognize; it stays addressable but its purpose is not described"
                ),
            )
        )
    if location is None:
        gaps.append(
            Unresolved(
                address=address,
                reason=UnresolvedReason.NO_LOCATION,
                message=(
                    f"{address!r} has no location on the deck, so an agent cannot reason "
                    "about where it is; assign it before serving"
                ),
            )
        )
    if grid is not None and grid.item_max_volume_ul is None and kind is not LabwareKind.TIP_RACK:
        gaps.append(
            Unresolved(
                address=address,
                reason=UnresolvedReason.NO_CAPACITY,
                message=(
                    f"{address!r} has {grid.item_count} items that report no maximum volume, "
                    "so overfilling cannot be caught before it happens"
                ),
            )
        )

    labware = DraftLabware(
        address=address,
        kind=kind,
        type_name=type(resource).__name__,
        model=getattr(resource, "model", None),
        location_mm=location,
        size_mm=(
            float(resource.get_size_x()),
            float(resource.get_size_y()),
            float(resource.get_size_z()),
        ),
        grid=grid,
    )
    return labware, gaps


def command_surface() -> list[DraftCommand]:
    """The commands the bridge exposes, with their safety classes.

    Fixed rather than derived, unlike the ophyd bridge: PyLabRobot's frontend
    is one class with one set of operations, so there is nothing to discover.
    Safety classes follow ``DESIGN.md``: anything that moves or consumes
    material is S2, reads are S0, and ``stop`` is S0 so recovery stays
    available while an interlock is tripped.

    Example:
        >>> next(c.safety_class for c in command_surface() if c.name == "aspirate")
        'S2'
    """
    return [
        DraftCommand(
            name="describe_deck",
            description=(
                "List the labware on the deck, the state of each pipetting channel, and "
                "the volume of every well known to hold liquid."
            ),
            safety_class="S0",
        ),
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
            name="return_tips",
            description="Return the mounted tips to the spots they were picked up from.",
            safety_class="S2",
        ),
        DraftCommand(
            name="discard_tips",
            description="Discard the mounted tips into the trash.",
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
            name="stop",
            description="Stop the liquid handler immediately.",
            safety_class="S0",
        ),
    ]


def introspect(liquid_handler: Any) -> DraftInstrument:
    """Derive a draft Labwire descriptor from a configured LiquidHandler.

    ``liquid_handler`` is typed ``Any`` deliberately: PyLabRobot ships no type
    information (there is no ``py.typed`` marker in 0.2.1), so static checking
    cannot verify the shape here.

    Example:
        >>> # introspect(lh).find("source_plate").grid.item_count
        >>> # 96
    """
    labware: list[DraftLabware] = []
    unresolved: list[Unresolved] = []
    for resource in addressable_resources(liquid_handler):
        described, gaps = _describe_labware(resource)
        labware.append(described)
        unresolved.extend(gaps)

    backend = getattr(liquid_handler, "backend", None)
    backend_class = type(backend).__name__ if backend is not None else "unknown"
    # num_channels is a backend constructor argument, so it is available before
    # setup(); the head trackers are not built until setup() has run.
    channel_count = int(
        getattr(backend, "num_channels", 0) or len(getattr(liquid_handler, "head", ()))
    )

    identity = IdentityInfo(
        manufacturer="PyLabRobot bridge (Labwire)",
        model=backend_class,
        serial_number=str(liquid_handler.name),
        firmware_version=f"pylabrobot {_pylabrobot_version()}",
    )
    return DraftInstrument(
        identity=identity,
        backend_class=backend_class,
        channel_count=channel_count,
        labware=labware,
        commands=command_surface(),
        unresolved=unresolved,
    )
