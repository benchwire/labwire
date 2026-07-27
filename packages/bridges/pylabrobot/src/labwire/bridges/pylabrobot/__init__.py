"""Expose a PyLabRobot liquid handler as a Labwire instrument.

PyLabRobot is an optional dependency, and the introspection and addressing
layers do not import it at all: they work against any object with the
resource-tree interface, so this package imports cleanly whether or not
PyLabRobot is installed. You need it to have a liquid handler to pass in.

Example:
    >>> from labwire.bridges.pylabrobot import Address
    >>> Address.parse("source_plate/A1").item
    'A1'
"""

from labwire.bridges.pylabrobot.addressing import (
    ADDRESS_PATTERN,
    Address,
    address_of,
    resolve,
    resolve_all,
)
from labwire.bridges.pylabrobot.annotations import (
    AnnotationError,
    AnnotationFile,
    CommandAnnotation,
    InstrumentAnnotation,
    ResourceAnnotation,
    annotation_for,
    check,
    load_annotations,
)
from labwire.bridges.pylabrobot.deck import (
    ChannelState,
    DeckState,
    LabwareState,
    WellContents,
    deck_state,
    locked_labware,
)
from labwire.bridges.pylabrobot.introspect import (
    DraftCommand,
    DraftInstrument,
    DraftLabware,
    Grid,
    LabwareKind,
    Unresolved,
    UnresolvedReason,
    addressable_resources,
    command_surface,
    introspect,
)

__all__ = [
    "ADDRESS_PATTERN",
    "Address",
    "AnnotationError",
    "AnnotationFile",
    "ChannelState",
    "CommandAnnotation",
    "DeckState",
    "DraftCommand",
    "DraftInstrument",
    "DraftLabware",
    "Grid",
    "InstrumentAnnotation",
    "LabwareKind",
    "LabwareState",
    "ResourceAnnotation",
    "Unresolved",
    "UnresolvedReason",
    "WellContents",
    "address_of",
    "addressable_resources",
    "annotation_for",
    "check",
    "command_surface",
    "deck_state",
    "introspect",
    "load_annotations",
    "locked_labware",
    "resolve",
    "resolve_all",
]
