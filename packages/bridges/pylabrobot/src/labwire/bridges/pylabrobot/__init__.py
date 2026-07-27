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
from labwire.bridges.pylabrobot.introspect import (
    DraftCommand,
    DraftInstrument,
    DraftLabware,
    Grid,
    LabwareKind,
    Unresolved,
    UnresolvedReason,
    command_surface,
    introspect,
)

__all__ = [
    "ADDRESS_PATTERN",
    "Address",
    "DraftCommand",
    "DraftInstrument",
    "DraftLabware",
    "Grid",
    "LabwareKind",
    "Unresolved",
    "UnresolvedReason",
    "address_of",
    "command_surface",
    "introspect",
    "resolve",
    "resolve_all",
]
