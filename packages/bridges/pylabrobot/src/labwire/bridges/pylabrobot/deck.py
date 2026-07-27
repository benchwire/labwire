"""The deck, projected small enough to hand an agent.

A populated STARlet deck serializes to about 133 KB across 208 resources,
almost all of it geometry nobody will reason about. This module is the
projection that makes deck state usable: what labware is loaded, what each
pipetting channel is holding, and which wells contain liquid.

Two choices are worth stating because they are not obvious.

**Well contents are sparse.** Listing all 96 wells of every plate would be
mostly zeroes, and the zeroes are the part an agent can infer. Only wells
believed to hold liquid appear, so a fresh deck projects to almost nothing and
a deck mid-protocol projects to exactly the interesting part.

**This is a command result, not a descriptor field and not telemetry.** A deck
changes between runs and during them, so a descriptor would be lying, and a
tree of hundreds of wells is not a stream of unit-bearing scalars. Labwire
v0.2 offers nowhere else to put it. See ``SPEC-FINDINGS.md``.

Example:
    >>> # deck_state(lh, AnnotationFile()).channels[0].has_tip
"""

from typing import Any

from labwire.bridges.pylabrobot.addressing import address_of
from labwire.bridges.pylabrobot.annotations import AnnotationFile, annotation_for
from labwire.bridges.pylabrobot.introspect import (
    DraftLabware,
    LabwareKind,
    addressable_resources,
    introspect,
)
from pydantic import BaseModel, ConfigDict


class ChannelState(BaseModel):
    """What one pipetting channel is holding.

    Example:
        >>> ChannelState(index=0, has_tip=False).has_tip
        False
    """

    model_config = ConfigDict(frozen=True)

    index: int
    has_tip: bool
    tip_max_volume_ul: float | None = None
    """Capacity of the mounted tip, which bounds a single aspiration."""


class WellContents(BaseModel):
    """One well believed to hold liquid.

    "Believed" is the right word: PyLabRobot's volume tracker knows what it
    has been told and what it has moved, and cannot see into a plate.

    Example:
        >>> WellContents(address="plate/A1", volume_ul=200.0).volume_ul
        200.0
    """

    model_config = ConfigDict(frozen=True)

    address: str
    volume_ul: float
    max_volume_ul: float | None = None


class LabwareState(BaseModel):
    """A piece of labware, with what the annotation file says about it.

    Example:
        >>> # state.find("acid_stock").hazard
    """

    model_config = ConfigDict(frozen=True)

    address: str
    kind: LabwareKind
    type_name: str
    model: str | None = None
    location_mm: tuple[float, float, float] | None = None
    grid: Any = None
    description: str | None = None
    hazard: str | None = None
    """What the annotation file says this holds, surfaced so an agent can see it."""
    safety_class: str | None = None
    """Effective class for operations touching this labware, when annotated."""
    locked: bool = False
    """Operations touching this labware are refused outright."""
    tips_available: int | None = None
    """For a tip rack: how many spots still hold a tip."""


class DeckState(BaseModel):
    """The whole projection: labware, channels, and liquid.

    Example:
        >>> # deck_state(lh, annotations).contents
    """

    model_config = ConfigDict(frozen=True)

    labware: list[LabwareState]
    channels: list[ChannelState]
    contents: list[WellContents]
    """Only wells believed to hold liquid; an empty deck projects an empty list."""

    def find(self, address: str) -> LabwareState:
        """Look up labware by address.

        Example:
            >>> # deck_state(lh, annotations).find("source_plate").kind
        """
        for candidate in self.labware:
            if candidate.address == address:
                return candidate
        raise KeyError(f"no such labware: {address!r}")


def _volume_of(well: Any) -> float | None:
    tracker = getattr(well, "tracker", None)
    if tracker is None:
        return None
    try:
        return float(tracker.get_used_volume())
    except Exception:  # pragma: no cover - a VolumeTracker always reports a volume
        return None


def _tips_available(rack: Any) -> int | None:
    if not hasattr(rack, "get_all_items"):
        return None
    available = 0
    for spot in rack.get_all_items():
        tracker = getattr(spot, "tracker", None)
        if tracker is not None and getattr(tracker, "has_tip", False):
            available += 1
    return available


def _channel_states(liquid_handler: Any) -> list[ChannelState]:
    head = getattr(liquid_handler, "head", None) or {}
    states: list[ChannelState] = []
    for index in sorted(head):
        tracker = head[index]
        has_tip = bool(getattr(tracker, "has_tip", False))
        capacity: float | None = None
        if has_tip:
            try:
                capacity = float(tracker.get_tip().maximal_volume)
            except Exception:  # pragma: no cover - a mounted tip always has a capacity
                capacity = None
        states.append(ChannelState(index=index, has_tip=has_tip, tip_max_volume_ul=capacity))
    return states


def _labware_state(resource: Any, draft: DraftLabware, annotations: AnnotationFile) -> LabwareState:
    annotation = annotation_for(
        annotations,
        name=draft.address,
        model=draft.model,
        type_name=draft.type_name,
    )
    return LabwareState(
        address=draft.address,
        kind=draft.kind,
        type_name=draft.type_name,
        model=draft.model,
        location_mm=draft.location_mm,
        grid=draft.grid,
        description=annotation.description,
        hazard=annotation.hazard,
        safety_class=annotation.safety_class,
        locked=annotation.locked,
        tips_available=(_tips_available(resource) if draft.kind is LabwareKind.TIP_RACK else None),
    )


def deck_state(liquid_handler: Any, annotations: AnnotationFile | None = None) -> DeckState:
    """Project the current deck into something an agent can read.

    Example:
        >>> # len(deck_state(lh).labware)
    """
    annotations = annotations or AnnotationFile()
    draft = introspect(liquid_handler)
    by_address = {item.address: item for item in draft.labware}

    labware: list[LabwareState] = []
    contents: list[WellContents] = []
    for resource in addressable_resources(liquid_handler):
        described = by_address.get(address_of(resource))
        if described is None:  # pragma: no cover - introspect covers the same set
            continue
        labware.append(_labware_state(resource, described, annotations))
        if described.kind is LabwareKind.TIP_RACK or not hasattr(resource, "get_all_items"):
            continue
        for item in resource.get_all_items():
            volume = _volume_of(item)
            if volume is None or volume <= 0.0:
                continue  # sparse: the empty wells are the ones you can infer
            maximum = getattr(item, "max_volume", None)
            contents.append(
                WellContents(
                    address=address_of(item),
                    volume_ul=volume,
                    max_volume_ul=float(maximum) if isinstance(maximum, int | float) else None,
                )
            )

    return DeckState(
        labware=labware,
        channels=_channel_states(liquid_handler),
        contents=contents,
    )


def locked_labware(annotations: AnnotationFile, resources: list[Any]) -> list[str]:
    """Names of the given resources that an annotation has locked.

    Items are checked through their parent, so locking a plate locks every
    well of it without naming 96 wells.

    Example:
        >>> locked_labware(AnnotationFile(), [])
        []
    """
    locked: list[str] = []
    for resource in resources:
        owner = resource
        parent = getattr(resource, "parent", None)
        if parent is not None and hasattr(parent, "get_child_identifier"):
            owner = parent
        annotation = annotation_for(
            annotations,
            name=str(owner.name),
            model=getattr(owner, "model", None),
            type_name=type(owner).__name__,
        )
        if annotation.locked and str(owner.name) not in locked:
            locked.append(str(owner.name))
    return locked
