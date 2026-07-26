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


def numeric_property_names(schema: dict[str, Any]) -> set[str]:
    """Names of numeric properties in a JSON Schema object (SPEC §7.2).

    Used to enforce that every quantity carries a UCUM unit code. Recognizes
    direct ``number``/``integer`` types and the ``anyOf``/``oneOf`` forms
    pydantic emits for optional numbers.

    Example:
        >>> numeric_property_names({"properties": {"v": {"type": "number"}}})
        {'v'}
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return set()
    numeric: set[str] = set()
    for raw_name, raw_prop in cast("dict[Any, Any]", properties).items():
        if not isinstance(raw_prop, dict):
            continue
        prop = cast("dict[str, Any]", raw_prop)
        declared: list[Any] = [prop.get("type")]
        for combinator in ("anyOf", "oneOf"):
            variants = prop.get(combinator)
            if isinstance(variants, list):
                for raw_variant in cast("list[Any]", variants):
                    if isinstance(raw_variant, dict):
                        declared.append(cast("dict[str, Any]", raw_variant).get("type"))
        if any(entry in ("number", "integer") for entry in declared):
            numeric.add(str(raw_name))
    return numeric


def _returns_unnamed_numbers(schema: dict[str, Any]) -> bool:
    """Whether a schema returns numbers without naming them (a mapping or array)."""
    for key in ("additionalProperties", "items"):
        member = schema.get(key)
        if isinstance(member, dict) and cast("dict[str, Any]", member).get("type") in (
            "number",
            "integer",
        ):
            return True
    return schema.get("type") in ("number", "integer")


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
        """Enforce SPEC §7.2: a quantity without a UCUM code is invalid."""
        missing = sorted(
            numeric_property_names(self.params_schema)
            - {name for name, code in self.unit_annotations.items() if code.strip()}
        )
        if missing:
            raise ValueError(
                f"command {self.name!r}: numeric parameter(s) {missing} lack UCUM unit "
                'codes in unit_annotations (use "1" for dimensionless quantities)'
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
