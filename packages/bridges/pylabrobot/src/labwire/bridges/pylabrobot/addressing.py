"""The bijection between PyLabRobot's resource tree and the URI space.

Until v0.3 this module owned an invented address grammar (``"plate/A1"``),
published as a JSON Schema pattern and checked at runtime. That grammar is
gone, replaced by the protocol's own scheme (SPEC §10.1): the deck resource
is ``labwire:deck``, labware standing on it is ``labwire:deck/<name>``, and
an item of that labware is ``labwire:deck/<name>/<id>``, composed by the one
protocol-defined rule from the read result's index. There is nothing
bridge-private left to learn.

What stayed is this module's discipline: one spelling per thing (PyLabRobot's
derived internal names are refused with the canonical URI), and every failure
names what would have worked.

Example:
    >>> split_deck_uri("labwire:deck/source_plate/A1")
    ('source_plate', 'A1')
"""

from typing import Any

from labwire.core.errors import ValidationError

DECK_URI = "labwire:deck"
"""The one resource the PyLabRobot bridge declares."""


def split_deck_uri(uri: object) -> tuple[str, str | None]:
    """Split a deck item URI into (labware name, item id or None).

    Raises :class:`ValidationError` for anything that is not a well-formed
    URI under ``labwire:deck``.

    Example:
        >>> split_deck_uri("labwire:deck/tips")
        ('tips', None)
    """
    if not isinstance(uri, str) or not uri.startswith(DECK_URI + "/"):
        raise ValidationError(
            f"malformed reference {uri!r}: expected '{DECK_URI}/<labware>' or "
            f"'{DECK_URI}/<labware>/<item>', for example '{DECK_URI}/source_plate/A1'"
        )
    rest = uri.removeprefix(DECK_URI + "/")
    if not rest or rest.endswith("/") or "//" in rest:
        raise ValidationError(
            f"malformed reference {uri!r}: empty path segment; expected "
            f"'{DECK_URI}/<labware>' or '{DECK_URI}/<labware>/<item>', for example "
            f"'{DECK_URI}/source_plate/A1'"
        )
    parts = rest.split("/")
    if not all(parts):
        raise ValidationError(
            f"malformed reference {uri!r}: empty path segment; for example "
            f"'{DECK_URI}/source_plate/A1'"
        )
    if len(parts) > 2:
        raise ValidationError(
            f"malformed reference {uri!r}: at most '{DECK_URI}/<labware>/<item>', "
            f"for example '{DECK_URI}/source_plate/A1'"
        )
    labware = parts[0]
    item = parts[1] if len(parts) == 2 else None
    return labware, item


def uri_of(resource: Any) -> str:
    """The canonical URI of a PyLabRobot resource on the deck.

    Items of an itemized resource (wells, tip spots) compose through their
    parent per the protocol rule; everything else is ``labwire:deck/<name>``.

    Example:
        >>> # uri_of(plate.get_item("A1")) -> 'labwire:deck/source_plate/A1'
    """
    parent = getattr(resource, "parent", None)
    if parent is not None and hasattr(parent, "get_child_identifier"):
        try:
            identifier = parent.get_child_identifier(resource)
        except Exception:  # not an item of that parent after all
            identifier = None
        if identifier is not None:
            return f"{DECK_URI}/{parent.name}/{identifier}"
    return f"{DECK_URI}/{resource.name}"


def _known_labware(root: Any) -> list[str]:
    names: list[str] = []
    for child in root.get_all_children():
        # Items are addressed through their parent, so listing all 96 wells of
        # every plate would bury the names a caller actually chooses.
        parent = getattr(child, "parent", None)
        if parent is not None and hasattr(parent, "get_child_identifier"):
            continue
        names.append(f"{DECK_URI}/{child.name}")
    return sorted(names)


def resolve(root: Any, uri: str) -> Any:
    """Resolve a deck URI to the live PyLabRobot object, or explain why not.

    The protocol server validates references before a handler runs
    (SPEC §10.4); this resolution is the handler's own step from a URI the
    server already vouched for to the object PyLabRobot needs. It keeps the
    full errors anyway, because a defensive layer that assumes the layer
    above is correct is not a defensive layer.

    Example:
        >>> # resolve(lh, "labwire:deck/source_plate/A1").name
        >>> # 'source_plate_well_A1'
    """
    labware_name, item = split_deck_uri(uri)
    try:
        labware = root.get_resource(labware_name)
    except Exception as exc:
        known = _known_labware(root)
        raise ValidationError(
            f"no labware named {labware_name!r} on the deck; "
            f"known labware: {', '.join(known) if known else '(none assigned)'}"
        ) from exc

    # PyLabRobot's get_resource searches the entire subtree by name, so a
    # derived name like 'source_plate_well_A1' resolves happily. Accepting it
    # would give every well two spellings; refusing it with the canonical one
    # keeps one way to say a thing.
    item_parent = getattr(labware, "parent", None)
    if item_parent is not None and hasattr(item_parent, "get_child_identifier"):
        raise ValidationError(
            f"{labware_name!r} is an item of {item_parent.name!r}, not labware in its "
            f"own right; address it as {uri_of(labware)!r}"
        )

    if item is None:
        return labware

    if not hasattr(labware, "get_item"):
        raise ValidationError(
            f"{labware_name!r} is a {type(labware).__name__}, which has no addressable "
            f"items, so {uri!r} cannot be resolved; address it as "
            f"'{DECK_URI}/{labware_name}' instead"
        )
    try:
        return labware.get_item(item)
    except Exception as exc:
        rows = getattr(labware, "num_items_y", None)
        columns = getattr(labware, "num_items_x", None)
        shape = f" ({rows} rows by {columns} columns)" if rows and columns else ""
        raise ValidationError(
            f"{labware_name!r} has no item {item!r}{shape}; items are addressed like 'A1'"
        ) from exc


def resolve_all(root: Any, uris: list[str]) -> list[Any]:
    """Resolve a list of URIs, failing on the first bad one.

    Example:
        >>> # resolve_all(lh, ["labwire:deck/plate/A1", "labwire:deck/plate/B1"])
    """
    return [resolve(root, uri) for uri in uris]
