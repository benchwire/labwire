"""Naming labware and its items across the wire.

PyLabRobot operations take live objects: a ``Well``, a ``TipSpot``, a
``Plate``. JSON-RPC carries JSON. This module is the whole of the translation
between the two, and it is deliberately the smallest, strictest thing that can
do the job.

An address is ``"<labware>"`` or ``"<labware>/<item>"``::

    source_plate        the plate itself
    source_plate/A1     one well of it
    tips/H12            one tip spot

PyLabRobot's own derived names (``source_plate_well_A1``) are not accepted,
and neither is its range syntax (``plate["A1:H1"]``). Both are explained in
`DESIGN.md <../../../DESIGN.md>`_: derived names leak a naming rule an agent
should not have to know, and ranges duplicate a cardinality mechanism JSON
arrays already provide.

Example:
    >>> Address.parse("source_plate/A1")
    Address(labware='source_plate', item='A1')
"""

import re
from dataclasses import dataclass
from typing import Any

from labwire.core.errors import ValidationError

_LABWARE = r"[A-Za-z0-9][A-Za-z0-9_.\-]*"
_ITEM = r"[A-Za-z0-9_]+"
ADDRESS_PATTERN = rf"^{_LABWARE}(/{_ITEM})?$"
"""JSON Schema ``pattern`` for an address parameter.

Shape only. Whether the address names something that exists on this deck is
not expressible in JSON Schema, and is checked at resolution time instead.
That gap is the subject of a SPEC-FINDINGS entry.
"""

_ADDRESS_RE = re.compile(ADDRESS_PATTERN)


@dataclass(frozen=True)
class Address:
    """A labware name, optionally with one item identifier.

    Example:
        >>> str(Address("tips", "A1"))
        'tips/A1'
    """

    labware: str
    item: str | None = None

    def __str__(self) -> str:
        return self.labware if self.item is None else f"{self.labware}/{self.item}"

    @classmethod
    def parse(cls, text: object) -> "Address":
        """Parse an address, raising :class:`ValidationError` if malformed.

        Typed ``object`` rather than ``str`` on purpose: addresses arrive as
        arbitrary JSON, so the type check is a runtime obligation, not an
        assumption a caller can be trusted to have met.

        Example:
            >>> Address.parse("plate").item is None
            True
        """
        if not isinstance(text, str) or not _ADDRESS_RE.match(text):
            raise ValidationError(
                f"malformed address {text!r}: expected 'labware' or 'labware/item', "
                "for example 'source_plate/A1'"
            )
        labware, _, item = text.partition("/")
        return cls(labware, item or None)


def address_of(resource: Any) -> str:
    """The canonical address of a PyLabRobot resource.

    Items of an itemized resource (wells, tip spots) address as
    ``parent/identifier``; everything else addresses by its own name.

    Example:
        >>> # address_of(plate.get_item("A1")) -> 'source_plate/A1'
    """
    parent = getattr(resource, "parent", None)
    if parent is not None and hasattr(parent, "get_child_identifier"):
        try:
            identifier = parent.get_child_identifier(resource)
        except Exception:  # not an item of that parent after all
            identifier = None
        if identifier is not None:
            return f"{parent.name}/{identifier}"
    return str(resource.name)


def _known_labware(root: Any) -> list[str]:
    names: list[str] = []
    for child in root.get_all_children():
        # Items are addressed through their parent, so listing all 96 wells of
        # every plate would bury the names a caller actually chooses.
        parent = getattr(child, "parent", None)
        if parent is not None and hasattr(parent, "get_child_identifier"):
            continue
        names.append(str(child.name))
    return sorted(names)


def resolve(root: Any, address: str | Address) -> Any:
    """Resolve an address against a deck, or explain precisely why it fails.

    ``root`` is any PyLabRobot resource that contains the target, normally the
    ``LiquidHandler`` or its deck. Every failure raises
    :class:`ValidationError` naming the address and what would have worked,
    because an agent that gets a bare "not found" has nothing to act on.

    Example:
        >>> # resolve(lh, "source_plate/A1").name -> 'source_plate_well_A1'
    """
    parsed = Address.parse(address) if isinstance(address, str) else address
    try:
        labware = root.get_resource(parsed.labware)
    except Exception as exc:
        known = _known_labware(root)
        raise ValidationError(
            f"no labware named {parsed.labware!r} on the deck; "
            f"known labware: {', '.join(known) if known else '(none assigned)'}"
        ) from exc

    # PyLabRobot's get_resource searches the entire subtree by name, so a
    # derived name like 'source_plate_well_A1' resolves happily. Accepting it
    # would give every well two spellings; refusing it with the canonical one
    # costs an agent a single retry and keeps one way to say a thing.
    item_parent = getattr(labware, "parent", None)
    if item_parent is not None and hasattr(item_parent, "get_child_identifier"):
        raise ValidationError(
            f"{parsed.labware!r} is an item of {item_parent.name!r}, not labware in its own "
            f"right; address it as {address_of(labware)!r}"
        )

    if parsed.item is None:
        return labware

    if not hasattr(labware, "get_item"):
        raise ValidationError(
            f"{parsed.labware!r} is a {type(labware).__name__}, which has no addressable "
            f"items, so {str(parsed)!r} cannot be resolved; address it as "
            f"{parsed.labware!r} instead"
        )
    try:
        return labware.get_item(parsed.item)
    except Exception as exc:
        rows = getattr(labware, "num_items_y", None)
        columns = getattr(labware, "num_items_x", None)
        shape = f" ({rows} rows by {columns} columns)" if rows and columns else ""
        raise ValidationError(
            f"{parsed.labware!r} has no item {parsed.item!r}{shape}; items are addressed like 'A1'"
        ) from exc


def resolve_all(root: Any, addresses: list[str]) -> list[Any]:
    """Resolve a list of addresses, failing on the first bad one.

    Example:
        >>> # resolve_all(lh, ["plate/A1", "plate/B1"])
    """
    return [resolve(root, address) for address in addresses]
