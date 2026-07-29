"""Instrument capability description models (SPEC §7).

Example:
    >>> from labwire.core.capabilities import IdentityInfo
    >>> IdentityInfo(
    ...     manufacturer="Labwire Project",
    ...     model="SimPump-100",
    ...     serial_number="SIM-0001",
    ...     firmware_version="0.1.0",
    ... ).model
    'SimPump-100'
"""

import re
from typing import Any, Literal, NamedTuple, Self, cast

from pydantic import BaseModel, ConfigDict, model_validator

SafetyClass = Literal["S0", "S1", "S2", "S3"]
"""Command risk classes (SPEC §8.6), a taxonomy adopted from LAP."""

KIND_REGISTRY: frozenset[str] = frozenset(
    {
        "deck",
        "labware",
        "plate",
        "tip_rack",
        "trough",
        "trash",
        "lid",
        "container",
        "tip_site",
        "site",
        "consumable",
    }
)
"""SPEC Appendix A. Kind names without a dot must come from here; anything
else must be vendor-prefixed (``<vendor>.<name>``). Seeded from the one
domain that forced the feature, and honest about it."""

_RESOURCE_URI = re.compile(r"^labwire:[A-Za-z0-9](?:[A-Za-z0-9_.\-]|%[0-9A-Fa-f]{2})*$")
"""A declared resource URI: ``labwire:`` plus one path segment (SPEC §10.1).

Items add segments; only single-segment URIs are declarable."""

_VENDOR_KIND = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*\.[A-Za-z][A-Za-z0-9_\-]*$")


def valid_kind(name: str) -> bool:
    """Whether a kind name is registered or well-formed vendor-prefixed.

    Example:
        >>> valid_kind("container"), valid_kind("acme.hotel_slot"), valid_kind("wells")
        (True, True, False)
    """
    if "." in name:
        return bool(_VENDOR_KIND.match(name))
    return name in KIND_REGISTRY


CancelSemantics = Literal["abort", "between_steps", "none"]
"""What command/cancel can honestly do to a running command (SPEC §8.3)."""

CONFIRMATION_REQUIRED_CLASSES: frozenset[str] = frozenset({"S2", "S3"})
"""Classes whose submissions require a confirmation value (SPEC §8.6)."""


_NUMERIC_TYPES = frozenset({"number", "integer"})
_MAX_SCHEMA_DEPTH = 24
"""Recursion guard. Schemas are trees, but ``$ref`` can make them cyclic."""

# Keywords whose value is a subschema applying to array items or mapping values.
# The name of the member is not knowable, so the path gets a positional marker.
_ITEM_KEYS = ("items", "additionalItems", "contains", "unevaluatedItems")
_VALUE_KEYS = ("additionalProperties", "unevaluatedProperties")
# Keywords whose value is a mapping of name to subschema.
_NAMED_KEYS = ("properties",)
_PATTERN_KEYS = ("patternProperties",)
# Keywords whose value is a subschema, or list of subschemas, at the same path.
_BRANCH_KEYS = ("anyOf", "oneOf", "allOf", "then", "else")
# Every keyword that constrains what an instance may contain. A node carrying
# none of them permits anything, numbers included.
_CONSTRAINING_KEYS = frozenset(
    (
        "type",
        "const",
        "enum",
        "$ref",
        *_ITEM_KEYS,
        *_VALUE_KEYS,
        *_NAMED_KEYS,
        *_PATTERN_KEYS,
        *_BRANCH_KEYS,
        "prefixItems",
        "not",
    )
)


class SchemaScan(NamedTuple):
    """Where numbers can appear in a schema, and where that cannot be decided.

    ``numeric`` holds a path per place a number may occur. ``opaque`` holds a
    path per place the schema declines to say, which is treated as a failure
    rather than as an absence: a schema that permits anything permits a
    quantity. ``units`` holds, for each numeric path, the value of the
    ``unit`` keyword on that node (SPEC §7.6), or None where absent.
    ``keyword_nodes`` holds ``(path, node)`` for every node carrying one of
    the keywords this protocol claims (``unit``, ``resource_ref``), so
    validators can enforce where each is and is not permitted.

    Example:
        >>> scan_schema({"properties": {"v": {"type": "number"}}}).numeric
        frozenset({'v'})
    """

    numeric: frozenset[str]
    opaque: frozenset[str]
    units: dict[str, str | None] = {}  # noqa: RUF012 - NamedTuple default, never mutated
    keyword_nodes: tuple[tuple[str, str, dict[str, Any]], ...] = ()
    """``(keyword, path, resolved node)`` per claimed-keyword occurrence."""


def _dict(value: Any) -> dict[str, Any] | None:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _pointer(reference: str, root: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a local RFC 6901 JSON pointer against the schema root.

    Covers ``#/$defs/X`` (pydantic), ``#/definitions/X`` (draft-07, which most
    non-Python generators emit), and nested pointers of any depth. A remote,
    dynamic, or unresolvable reference returns ``None`` and is treated as
    opaque by the caller, because a reference this code cannot follow is a
    reference whose contents it cannot vouch for.
    """
    if not reference.startswith("#"):
        return None
    node: Any = root
    for raw in reference.removeprefix("#").strip("/").split("/"):
        if not raw:
            continue
        token = raw.replace("~1", "/").replace("~0", "~")
        current = _dict(node)
        if current is None or token not in current:
            return None
        node = current[token]
    return _dict(node)


def _resolve(
    schema: dict[str, Any], root: dict[str, Any], seen: frozenset[str]
) -> tuple[dict[str, Any] | None, frozenset[str]]:
    """Follow a ``$ref`` chain, merging siblings, or fail closed.

    Returns ``(None, seen)`` when the chain cannot be followed. Siblings of a
    ``$ref`` are legal in 2020-12 and are merged rather than discarded, so
    ``{"$ref": ..., "type": "number"}`` keeps its type even if the reference
    is stale.
    """
    node = schema
    depth = 0
    while True:
        if "$dynamicRef" in node:
            return None, seen
        reference = node.get("$ref")
        if not isinstance(reference, str):
            return node, seen
        if reference in seen or depth > _MAX_SCHEMA_DEPTH:
            return node, seen  # a cycle contributes nothing new
        target = _pointer(reference, root)
        if target is None:
            return None, seen
        seen = seen | {reference}
        depth += 1
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        node = {**target, **siblings} if siblings else target


def _declares_number(schema: dict[str, Any]) -> bool:
    """Whether this one schema node is itself a number.

    JSON Schema has more than one way to say so, and a descriptor arriving
    over the wire was not necessarily written by pydantic: ``type`` may be a
    list, and ``const``/``enum`` pin a value without naming a type at all.
    """
    declared = schema.get("type")
    if isinstance(declared, str) and declared in _NUMERIC_TYPES:
        return True
    if isinstance(declared, list) and any(
        entry in _NUMERIC_TYPES for entry in cast("list[Any]", declared)
    ):
        return True
    if (
        "const" in schema
        and isinstance(schema["const"], int | float)
        and not isinstance(schema["const"], bool)
    ):
        return True
    choices = schema.get("enum")
    return isinstance(choices, list) and any(
        isinstance(choice, int | float) and not isinstance(choice, bool)
        for choice in cast("list[Any]", choices)
    )


_CLAIMED_KEYWORDS = ("unit", "resource_ref")


def _walk(
    node: Any,
    root: dict[str, Any],
    path: str,
    depth: int,
    seen: frozenset[str],
    numeric: set[str],
    opaque: set[str],
    units: dict[str, str | None],
    claimed: list[tuple[str, str, dict[str, Any]]],
    inherited_unit: str | None = None,
) -> None:
    """Record every path at which a number may appear, or which cannot be read."""
    if node is False:
        return  # a schema matching nothing
    if node is True:
        opaque.add(path)
        return
    schema = _dict(node)
    if schema is None or depth > _MAX_SCHEMA_DEPTH:
        opaque.add(path)
        return

    resolved, seen = _resolve(schema, root, seen)
    if resolved is None:
        opaque.add(path)  # fail closed on a reference we cannot follow
        return

    if _declares_number(resolved):
        numeric.add(path)
        declared_unit = resolved.get("unit", inherited_unit)
        if path not in units or units[path] is None:
            units[path] = declared_unit if isinstance(declared_unit, str) else None
    for keyword in _CLAIMED_KEYWORDS:
        if keyword in resolved:
            claimed.append((keyword, path, resolved))

    structural = _CONSTRAINING_KEYS & resolved.keys()
    if not structural and not _declares_number(resolved):
        opaque.add(path)  # an unconstrained node permits a number
        return

    own_unit = resolved.get("unit")
    branch_unit = own_unit if isinstance(own_unit, str) else inherited_unit
    for key in _BRANCH_KEYS:
        member = resolved.get(key)
        if isinstance(member, list):
            for raw in cast("list[Any]", member):
                _walk(
                    raw,
                    root,
                    path,
                    depth + 1,
                    seen,
                    numeric,
                    opaque,
                    units,
                    claimed,
                    branch_unit,
                )
        elif member is not None:
            _walk(
                member,
                root,
                path,
                depth + 1,
                seen,
                numeric,
                opaque,
                units,
                claimed,
                branch_unit,
            )

    for key in _NAMED_KEYS:
        members = _dict(resolved.get(key))
        if members is not None:
            for name, member in members.items():
                child = f"{path}.{name}" if path else str(name)
                _walk(member, root, child, depth + 1, seen, numeric, opaque, units, claimed)

    for key in _PATTERN_KEYS:
        members = _dict(resolved.get(key))
        if members is not None:
            for member in members.values():
                _walk(member, root, f"{path}{{}}", depth + 1, seen, numeric, opaque, units, claimed)

    prefix_items = resolved.get("prefixItems")
    if isinstance(prefix_items, list):
        for index, member in enumerate(cast("list[Any]", prefix_items)):
            _walk(
                member, root, f"{path}[{index}]", depth + 1, seen, numeric, opaque, units, claimed
            )

    for key in _ITEM_KEYS:
        member = resolved.get(key)
        if isinstance(member, list):  # draft-07 tuple form of `items`
            for index, entry in enumerate(cast("list[Any]", member)):
                _walk(
                    entry,
                    root,
                    f"{path}[{index}]",
                    depth + 1,
                    seen,
                    numeric,
                    opaque,
                    units,
                    claimed,
                )
        elif member is not None:
            _walk(member, root, f"{path}[]", depth + 1, seen, numeric, opaque, units, claimed)

    for key in _VALUE_KEYS:
        member = resolved.get(key)
        if member is not None:
            _walk(member, root, f"{path}{{}}", depth + 1, seen, numeric, opaque, units, claimed)

    # An object that neither names its properties nor closes the door on extra
    # ones can carry a quantity under a name nobody declared.
    declared_type = resolved.get("type")
    is_object = declared_type == "object" or (
        isinstance(declared_type, list) and "object" in cast("list[Any]", declared_type)
    )
    if is_object and "additionalProperties" not in resolved and "$ref" not in schema:
        opaque.add(f"{path}{{}}")
    is_array = declared_type == "array" or (
        isinstance(declared_type, list) and "array" in cast("list[Any]", declared_type)
    )
    if is_array and not ({"items", "prefixItems", "contains"} & resolved.keys()):
        opaque.add(f"{path}[]")


def scan_schema(schema: dict[str, Any], root: dict[str, Any] | None = None) -> SchemaScan:
    """Find every path where a number may appear, and every unreadable one.

    Example:
        >>> scan_schema({"type": "array", "items": {"type": "number"}}).numeric
        frozenset({'[]'})
    """
    numeric: set[str] = set()
    opaque: set[str] = set()
    units: dict[str, str | None] = {}
    claimed: list[tuple[str, str, dict[str, Any]]] = []
    _walk(
        schema,
        root if root is not None else schema,
        "",
        0,
        frozenset(),
        numeric,
        opaque,
        units,
        claimed,
    )
    return SchemaScan(frozenset(numeric), frozenset(opaque), units, tuple(claimed))


def carries_number(schema: dict[str, Any], root: dict[str, Any] | None = None) -> bool:
    """Whether a value matching this schema can contain a number (SPEC §7.2).

    Example:
        >>> carries_number({"type": "array", "items": {"type": "number"}})
        True
        >>> carries_number({"type": "array", "items": {"type": "string"}})
        False
    """
    scan = scan_schema(schema, root)
    return bool(scan.numeric or scan.opaque)


def _top_level(path: str) -> str:
    """The parameter name a path belongs to, or "" when it has none."""
    head = path.split(".", 1)[0]
    for marker in ("[", "{"):
        head = head.split(marker, 1)[0]
    return head


def _is_named(path: str) -> bool:
    """Whether a path names a field, so a matching code can be demanded.

    A path beginning with a container marker still names one: ``[].volume_ul``
    is a volume just as much as ``readings[].volume_ul`` is, and an earlier
    version exempted the first because its head was empty. Deleting a wrapper
    model must not switch the check off. Only a genuinely anonymous quantity,
    such as a bare number or an array of them, has no field to match.
    """
    return "." in path or bool(_top_level(path))


def _covered(path: str, keys: set[str]) -> bool:
    """Whether an annotation key names this path or an ancestor of it."""
    if path in keys:
        return True
    return any(
        path.startswith(key) and path[len(key) :][:1] in (".", "[", "{") for key in keys if key
    )


def numeric_property_names(schema: dict[str, Any]) -> set[str]:
    """Names of parameters that carry a quantity, at any depth (SPEC §7.2).

    Example:
        >>> sorted(numeric_property_names({"properties": {
        ...     "v": {"type": "number"},
        ...     "vs": {"type": "array", "items": {"type": "number"}},
        ...     "name": {"type": "string"},
        ... }}))
        ['v', 'vs']
    """
    return {name for path in scan_schema(schema).numeric if (name := _top_level(path))}


class _SpecModel(BaseModel):
    """Base for descriptor models: unknown fields tolerated and preserved."""

    model_config = ConfigDict(extra="allow")


class IdentityInfo(_SpecModel):
    """Instrument identity (SPEC §7.1); embedded verbatim in manifests (§13).

    Example:
        >>> IdentityInfo(
        ...     manufacturer="m", model="d", serial_number="s", firmware_version="1"
        ... ).firmware_hash is None
        True
    """

    manufacturer: str
    model: str
    serial_number: str
    firmware_version: str
    firmware_hash: str | None = None


class CommandSpec(_SpecModel):
    """A declared command (SPEC §7.2).

    Every numeric parameter and every named numeric result field MUST carry a
    UCUM unit code; validation rejects a declaration that omits one, so an
    agent can never receive an ambiguous quantity.

    Example:
        >>> CommandSpec(
        ...     name="go",
        ...     title="Go",
        ...     description="Run.",
        ...     params_schema={"type": "object", "additionalProperties": False},
        ...     interruptible=False,
        ... ).safety_class
        'S1'
    """

    name: str
    title: str
    description: str
    params_schema: dict[str, Any]
    unit_annotations: dict[str, str] = {}
    returns_units: dict[str, str] = {}
    qudt_quantity_kind: dict[str, str] = {}
    safety_class: SafetyClass = "S1"
    returns_schema: dict[str, Any] | None = None
    estimated_duration_s: float | None = None
    cancel_semantics: CancelSemantics = "none"
    clears_interlocks: list[str] = []

    @model_validator(mode="after")
    def _require_unit_codes(self) -> Self:
        """Enforce SPEC §7.2: a quantity without a UCUM code is invalid.

        A quantity counts wherever it appears, so an eight-channel command
        taking a list of volumes is annotated exactly like a one-channel
        command taking one. A schema that declines to say what it contains is
        refused rather than assumed empty, because a schema permitting
        anything permits a quantity.
        """
        self._check_schema(self.params_schema, "parameter", self.unit_annotations, nested_ok=False)
        if self.returns_schema is not None:
            self._check_schema(
                self.returns_schema, "result field", self.returns_units, nested_ok=True
            )
        return self

    def _check_schema(
        self, schema: dict[str, Any], label: str, annotations: dict[str, str], *, nested_ok: bool
    ) -> None:
        scan = scan_schema(schema)
        declared = {name for name, code in annotations.items() if code.strip()}

        if scan.opaque:
            where = sorted(path or "<the whole value>" for path in scan.opaque)
            raise ValueError(
                f"command {self.name!r}: the {label} schema does not say what "
                f"{'is' if len(where) == 1 else 'are'} at {where}, so a quantity could travel "
                "there unannotated. Declare a concrete type; an open mapping, an untyped "
                "value, or a reference this build cannot resolve cannot be checked"
            )

        # A mapping of numbers is a declaration of arbitrarily many quantities
        # under names the schema never states, so one code cannot describe
        # them. This is the same objection the nested-object rule makes;
        # withholding the field names must not remove it.
        unnamed_mapping = sorted(path for path in scan.numeric if "{}" in path)
        if unnamed_mapping:
            where = [
                path.replace("{}", "{any key}") or "the whole value" for path in unnamed_mapping
            ]
            raise ValueError(
                f"command {self.name!r}: {where} are quantities under names the {label} schema "
                "does not declare, and one unit code cannot describe quantities of different "
                "kinds. Declare a model with named fields, one unit code each"
            )

        nested = sorted(path for path in scan.numeric if "." in path)
        if nested and not nested_ok:
            raise ValueError(
                f"command {self.name!r}: {label}(s) {nested} are quantities nested inside an "
                f"object, and unit codes are declared per {label}, so there is no way to "
                f"annotate them. Flatten them into separate {label}s, one unit code each"
            )

        missing = sorted(
            path for path in scan.numeric if _is_named(path) and not _covered(path, declared)
        )
        if missing:
            raise ValueError(
                f"command {self.name!r}: {label}(s) {missing} lack UCUM unit codes "
                f'(use "1" for dimensionless quantities). A quantity needs one wherever it '
                "appears, including inside a list"
            )
        # A quantity with no name of its own, such as a bare mapping or array
        # of numbers, cannot be matched to a key, so any code at all is asked
        # for rather than a matching one.
        if any(not _is_named(path) for path in scan.numeric) and not declared:
            raise ValueError(
                f"command {self.name!r}: the {label} carries numbers that name no field, so "
                "at least one UCUM unit code must be declared"
            )
        self._check_claimed_keywords(scan, label)

    def _check_claimed_keywords(self, scan: SchemaScan, label: str) -> None:
        """Enforce SPEC §7.2/§7.6: where `resource_ref` and `unit` may appear."""
        for keyword, path, node in scan.keyword_nodes:
            where = path or "<the whole value>"
            if keyword == "unit":
                raise ValueError(
                    f"command {self.name!r}: the 'unit' schema keyword at {where} is "
                    f"scoped to resource content schemas (SPEC 7.6); {label} units are "
                    "declared in unit_annotations and returns_units"
                )
            if label != "parameter":
                raise ValueError(
                    f"command {self.name!r}: 'resource_ref' at {where} is permitted only "
                    "inside params_schema in v0.3 (SPEC 7.2)"
                )
            ref = _dict(node.get("resource_ref"))
            kind = ref.get("kind") if ref else None
            enumerated_by = ref.get("enumerated_by") if ref else None
            if (
                ref is None
                or not isinstance(kind, str)
                or not isinstance(enumerated_by, str)
                or not kind.strip()
                or not enumerated_by.strip()
            ):
                raise ValueError(
                    f"command {self.name!r}: resource_ref at {where} must be an object "
                    "with non-empty string members 'kind' and 'enumerated_by' (SPEC 7.2)"
                )
            if not valid_kind(kind):
                raise ValueError(
                    f"command {self.name!r}: resource_ref at {where} declares kind "
                    f"{kind!r}, which is neither in the kind registry (SPEC Appendix A) "
                    "nor vendor-prefixed as '<vendor>.<name>'"
                )
            if node.get("type") != "string":
                raise ValueError(
                    f"command {self.name!r}: resource_ref at {where} must sit on a "
                    "string-typed node; a reference is a URI"
                )
            if "pattern" in node:
                raise ValueError(
                    f"command {self.name!r}: resource_ref at {where} must not share its "
                    "node with a 'pattern': a pattern is satisfiable by invention, which "
                    "is the failure typed references exist to remove (SPEC 7.2)"
                )

    def references(self) -> list[tuple[str, dict[str, str]]]:
        """Every ``(path, resource_ref)`` declared in this command's parameters.

        Example:
            >>> CommandSpec(
            ...     name="go", title="Go", description="Go.",
            ...     params_schema={"type": "object", "additionalProperties": False},
            ...     interruptible=False,
            ... ).references()
            []
        """
        scan = scan_schema(self.params_schema)
        return [
            (path, cast("dict[str, str]", node["resource_ref"]))
            for keyword, path, node in scan.keyword_nodes
            if keyword == "resource_ref"
        ]


class ChannelSpec(_SpecModel):
    """A typed measurement channel (SPEC §7.3).

    Example:
        >>> ChannelSpec(name="mass", description="w", dtype="float64", unit="g").unit
        'g'
    """

    name: str
    description: str
    dtype: Literal["float64", "int64", "bool", "string"]
    unit: str
    qudt_quantity_kind: str | None = None
    sample_rate_hz_hint: float | None = None

    @model_validator(mode="after")
    def _require_unit_code(self) -> Self:
        """Enforce SPEC §7.3: the channel unit is a non-empty UCUM code."""
        if not self.unit.strip():
            raise ValueError(
                f"channel {self.name!r}: unit must be a non-empty UCUM code "
                '(use "1" for dimensionless channels)'
            )
        return self


class ResourceSpec(_SpecModel):
    """A declared resource (SPEC §7.6): addressable, typed, readable state.

    Field-shape validation only in this milestone; the content-schema unit
    rule and reference-closure checks land with the server implementation.

    Example:
        >>> ResourceSpec(
        ...     uri="labwire:syringe", kind="consumable", title="Syringe",
        ...     description="The installed syringe.", item_kinds=[],
        ...     revision="r-1", content_schema={"type": "object",
        ...     "additionalProperties": False},
        ... ).kind
        'consumable'
    """

    uri: str
    kind: str
    title: str
    description: str
    item_kinds: list[str]
    revision: str
    content_schema: dict[str, Any]

    @model_validator(mode="after")
    def _well_formed(self) -> Self:
        """Enforce SPEC §7.6: URI shape, kind names, and content units."""
        if not _RESOURCE_URI.match(self.uri):
            raise ValueError(
                f"resource uri {self.uri!r} is not a declarable labwire: URI: expected "
                "'labwire:' plus one path segment, like 'labwire:deck' (items add "
                "segments and are not declared)"
            )
        for label, name in (
            ("kind", self.kind),
            *(("item_kinds entry", k) for k in self.item_kinds),
        ):
            if not valid_kind(name):
                raise ValueError(
                    f"resource {self.uri!r}: {label} {name!r} is neither in the kind "
                    "registry (SPEC Appendix A) nor vendor-prefixed as '<vendor>.<name>'"
                )
        if not self.revision.strip():
            raise ValueError(f"resource {self.uri!r}: revision must be non-empty")
        scan = scan_schema(self.content_schema)
        if scan.opaque:
            where = sorted(path or "<the whole value>" for path in scan.opaque)
            raise ValueError(
                f"resource {self.uri!r}: content_schema does not say what is at {where}; "
                "the closed-schema rule of SPEC 7.2 applies to content too"
            )
        unitless = sorted(
            path or "<the whole value>"
            for path in scan.numeric
            if not (scan.units.get(path) or "").strip()
        )
        if unitless:
            raise ValueError(
                f"resource {self.uri!r}: numeric content at {unitless} carries no 'unit' "
                'keyword (SPEC 7.6; use "1" for dimensionless). Resource content is '
                "state, state carries quantities, and a units-optional state format "
                "would reopen the hole 7.2 closed"
            )
        misplaced = [
            (keyword, path)
            for keyword, path, _node in scan.keyword_nodes
            if keyword == "resource_ref"
        ]
        if misplaced:
            raise ValueError(
                f"resource {self.uri!r}: 'resource_ref' is not permitted inside "
                f"content_schema (found at {[p or '<root>' for _k, p in misplaced]}); "
                "references are a parameter concept (SPEC 7.2)"
            )
        return self


class InterlockSpec(_SpecModel):
    """A declared safety interlock (SPEC §7.4).

    Example:
        >>> InterlockSpec(name="i", description="d", kind="hard", tripped=False).kind
        'hard'
    """

    name: str
    description: str
    kind: Literal["hard", "soft"]
    tripped: bool


class InstrumentDescriptor(_SpecModel):
    """Everything a client needs to operate the instrument (SPEC §7.1).

    Example:
        >>> desc = InstrumentDescriptor(
        ...     identity=IdentityInfo(
        ...         manufacturer="m", model="d", serial_number="s", firmware_version="1"
        ...     ),
        ...     commands=[],
        ...     channels=[],
        ...     interlocks=[],
        ... )
        >>> desc.max_concurrent_commands
        1
    """

    identity: IdentityInfo
    commands: list[CommandSpec]
    channels: list[ChannelSpec]
    interlocks: list[InterlockSpec]
    resources: list[ResourceSpec] = []
    """REQUIRED of v0.3 servers (SPEC §7.1); tolerated absent on receipt."""
    max_concurrent_commands: int = 1

    @model_validator(mode="after")
    def _references_closed(self) -> Self:
        """Enforce SPEC §7.6: the reference graph is closed.

        Every ``resource_ref.enumerated_by`` names a declared resource whose
        ``item_kinds`` contains the declared kind. Checked here so it holds
        both for a server about to serve the descriptor and for a client
        that just received one: an agent pointed at a resource that does not
        exist has a discovery story that dead-ends.
        """
        by_uri = {spec.uri: spec for spec in self.resources}
        for command_spec in self.commands:
            for path, ref in command_spec.references():
                target = by_uri.get(ref["enumerated_by"])
                if target is None:
                    raise ValueError(
                        f"command {command_spec.name!r}: resource_ref at "
                        f"{path or '<the whole value>'} is enumerated_by "
                        f"{ref['enumerated_by']!r}, which this descriptor does not "
                        f"declare (declared: {sorted(by_uri) or '(none)'})"
                    )
                if ref["kind"] not in target.item_kinds:
                    raise ValueError(
                        f"command {command_spec.name!r}: resource_ref at "
                        f"{path or '<the whole value>'} expects kind {ref['kind']!r}, "
                        f"which {target.uri!r} does not index "
                        f"(item_kinds: {target.item_kinds})"
                    )
        return self
