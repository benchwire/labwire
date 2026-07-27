"""Expose a PyLabRobot liquid handler as a Labwire instrument.

PyLabRobot is an optional dependency, and the introspection and addressing
layers do not import it at all: they work against any object with the
resource-tree interface, so this package imports cleanly whether or not
PyLabRobot is installed. You need it to have a liquid handler to pass in.

Example:
    >>> from labwire.bridges.pylabrobot import split_deck_uri
    >>> split_deck_uri("labwire:deck/source_plate/A1")
    ('source_plate', 'A1')
"""

from labwire.bridges.pylabrobot.addressing import (
    DECK_URI,
    resolve,
    resolve_all,
    split_deck_uri,
    uri_of,
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
from labwire.bridges.pylabrobot.bridge import PyLabRobotInstrument, map_error
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
    "DECK_URI",
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
    "PyLabRobotInstrument",
    "ResourceAnnotation",
    "Unresolved",
    "UnresolvedReason",
    "WellContents",
    "addressable_resources",
    "annotation_for",
    "check",
    "command_surface",
    "deck_state",
    "introspect",
    "load_annotations",
    "locked_labware",
    "map_error",
    "resolve",
    "resolve_all",
    "split_deck_uri",
    "uri_of",
]
