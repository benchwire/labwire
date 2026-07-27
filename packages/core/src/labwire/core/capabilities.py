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

from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, model_validator

SafetyClass = Literal["S0", "S1", "S2", "S3"]
"""Command risk classes (SPEC §8.6), a taxonomy adopted from LAP."""

CONFIRMATION_REQUIRED_CLASSES: frozenset[str] = frozenset({"S2", "S3"})
"""Classes whose submissions require a confirmation value (SPEC §8.6)."""


_NUMERIC_TYPES = frozenset({"number", "integer"})
_MAX_SCHEMA_DEPTH = 12
"""Recursion guard. Schemas are trees, but ``$ref`` can make them cyclic."""


def _dict(value: Any) -> dict[str, Any] | None:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Follow a local ``$ref`` into the schema's own ``$defs``.

    pydantic emits a ``$ref`` for any parameter whose type is a model, so a
    number nested inside one is invisible without this.
    """
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        return schema
    definitions = _dict(root.get("$defs")) or {}
    return _dict(definitions.get(reference.removeprefix("#/$defs/"))) or {}


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


def _branches(schema: dict[str, Any], root: dict[str, Any]) -> list[dict[str, Any]]:
    """A schema and every branch of its ``anyOf``/``oneOf``/``allOf``.

    pydantic writes ``float | None`` as an ``anyOf``, so an optional array of
    numbers hides one level deeper than an outright one.
    """
    resolved = _resolve(schema, root)
    found = [resolved]
    for combinator in ("anyOf", "oneOf", "allOf"):
        variants = resolved.get(combinator)
        if isinstance(variants, list):
            for raw in cast("list[Any]", variants):
                variant = _dict(raw)
                if variant is not None:
                    found.append(_resolve(variant, root))
    return found


def carries_number(schema: dict[str, Any], root: dict[str, Any], depth: int = 0) -> bool:
    """Whether a value matching this schema can contain a number (SPEC §7.2).

    Looks through the containers that hold quantities of a single kind: array
    items, fixed-length tuples, and mapping values, at any nesting depth. It
    deliberately does **not** look inside a nested object's named properties,
    because one unit code cannot describe fields of different kinds; that case
    is refused separately by :func:`nested_numeric_fields`.

    Example:
        >>> carries_number({"type": "array", "items": {"type": "number"}}, {})
        True
        >>> carries_number({"type": "array", "items": {"type": "string"}}, {})
        False
    """
    if depth > _MAX_SCHEMA_DEPTH:  # pragma: no cover - guards pathological schemas
        return False
    for branch in _branches(schema, root):
        if _declares_number(branch):
            return True
        for key in ("items", "additionalProperties", "contains"):
            member = _dict(branch.get(key))
            if member is not None and carries_number(member, root, depth + 1):
                return True
        prefix_items = branch.get("prefixItems")  # a fixed-length tuple
        if isinstance(prefix_items, list):
            for raw in cast("list[Any]", prefix_items):
                member = _dict(raw)
                if member is not None and carries_number(member, root, depth + 1):
                    return True
    return False


def nested_numeric_fields(schema: dict[str, Any], root: dict[str, Any]) -> list[str]:
    """Numeric fields of a nested object, which v0.2 cannot annotate.

    ``unit_annotations`` is keyed by parameter name, so a parameter whose type
    is an object with numeric fields has no way to say that ``x`` is in
    millimetres while ``pressure`` is in kilopascals. Rather than let such a
    quantity through unannotated, the declaration is refused.

    Example:
        >>> nested_numeric_fields({"type": "object", "properties": {}}, {})
        []
    """
    fields: list[str] = []
    for branch in _branches(schema, root):
        properties = _dict(branch.get("properties"))
        if properties is None:
            continue
        for name, raw in properties.items():
            member = _dict(raw)
            if member is not None and carries_number(member, root):
                fields.append(str(name))
    return sorted(set(fields))


def numeric_property_names(schema: dict[str, Any]) -> set[str]:
    """Names of properties that carry a quantity, at any depth (SPEC §7.2).

    Used to enforce that every quantity carries a UCUM unit code. A bare
    ``number``, an array of them, an optional array, a tuple, and a mapping of
    them all count: the unit belongs to the quantity, not to the container it
    arrived in.

    Example:
        >>> sorted(numeric_property_names({"properties": {
        ...     "v": {"type": "number"},
        ...     "vs": {"type": "array", "items": {"type": "number"}},
        ...     "name": {"type": "string"},
        ... }}))
        ['v', 'vs']
    """
    properties = _dict(schema.get("properties"))
    if properties is None:
        return set()
    return {
        str(name)
        for name, raw in properties.items()
        if (member := _dict(raw)) is not None and carries_number(member, schema)
    }


def nested_numeric_properties(schema: dict[str, Any]) -> dict[str, list[str]]:
    """Parameters whose type is an object with numeric fields, by name.

    Example:
        >>> nested_numeric_properties({"properties": {"a": {"type": "number"}}})
        {}
    """
    properties = _dict(schema.get("properties"))
    if properties is None:
        return {}
    found: dict[str, list[str]] = {}
    for name, raw in properties.items():
        member = _dict(raw)
        if member is None:
            continue
        fields = nested_numeric_fields(member, schema)
        if fields:
            found[str(name)] = fields
    return found


def _returns_unnamed_numbers(schema: dict[str, Any]) -> bool:
    """Whether a schema returns numbers without naming them (a mapping or array).

    Example:
        >>> _returns_unnamed_numbers({"type": "array", "items": {"type": "number"}})
        True
    """
    if _dict(schema.get("properties")) is not None:
        return False  # named fields are checked one by one instead
    return carries_number(schema, schema)


class _SpecModel(BaseModel):
    """Base for descriptor models: unknown fields tolerated and preserved."""

    model_config = ConfigDict(extra="allow")


class IdentityInfo(_SpecModel):
    """Instrument identity (SPEC §7.1); embedded verbatim in manifests (§12).

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
    interruptible: bool
    clears_interlocks: list[str] = []

    @model_validator(mode="after")
    def _require_unit_codes(self) -> Self:
        """Enforce SPEC §7.2: a quantity without a UCUM code is invalid.

        A quantity counts wherever it appears, including inside arrays,
        tuples, and mappings, so an eight-channel command taking a list of
        volumes is annotated exactly like a one-channel command taking one.
        """
        for label, schema in (
            ("parameter", self.params_schema),
            ("result field", self.returns_schema),
        ):
            if schema is None:
                continue
            for name, fields in nested_numeric_properties(schema).items():
                raise ValueError(
                    f"command {self.name!r}: {label} {name!r} is an object with numeric "
                    f"field(s) {fields}, and unit codes are declared per {label}, so there "
                    "is no way to annotate them. Flatten them into separate "
                    f"{label}s, one unit code each"
                )
        missing = sorted(
            numeric_property_names(self.params_schema)
            - {name for name, code in self.unit_annotations.items() if code.strip()}
        )
        if missing:
            raise ValueError(
                f"command {self.name!r}: numeric parameter(s) {missing} lack UCUM unit "
                'codes in unit_annotations (use "1" for dimensionless quantities). '
                "A quantity needs one wherever it appears, including inside a list"
            )
        if self.returns_schema is not None:
            declared_results = {name for name, code in self.returns_units.items() if code.strip()}
            missing_results = sorted(numeric_property_names(self.returns_schema) - declared_results)
            if missing_results:
                raise ValueError(
                    f"command {self.name!r}: numeric result field(s) {missing_results} "
                    "lack UCUM unit codes in returns_units"
                )
            # A mapping return (e.g. dict[str, float]) names no properties, so the
            # field-by-field check above cannot see it; require the author to declare
            # the units of the numbers they return.
            if not declared_results and _returns_unnamed_numbers(self.returns_schema):
                raise ValueError(
                    f"command {self.name!r}: returns numeric result field(s) but declares no "
                    "UCUM unit codes in returns_units"
                )
        return self


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
    max_concurrent_commands: int = 1
