"""Expose ophyd (Bluesky) devices as Labwire instruments.

ophyd abstracts hardware for Python programmers; Labwire describes hardware
to AI agents. This bridge composes the two — it does not reimplement drivers.
ophyd is an optional dependency: import this package only where it is
installed.

Example:
    >>> from ophyd.sim import SynAxis
    >>> from labwire.bridges.ophyd import introspect
    >>> introspect(SynAxis(name="ax")).identity.model
    'SynAxis'
"""

from labwire.bridges.ophyd._egu import egu_to_ucum
from labwire.bridges.ophyd.introspect import (
    ComponentRole,
    DraftCommand,
    DraftComponent,
    DraftInstrument,
    Unresolved,
    UnresolvedReason,
    introspect,
)

__all__ = [
    "ComponentRole",
    "DraftCommand",
    "DraftComponent",
    "DraftInstrument",
    "Unresolved",
    "UnresolvedReason",
    "egu_to_ucum",
    "introspect",
]
