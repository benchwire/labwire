"""Minimal-parameter synthesis from a command's params_schema.

The safety checks need parameters that pass SCHEMA validation, because the
spec's refusal precedence (SPEC 12.1) puts `validation` before the safety
refusals: schema-invalid parameters would trip the wrong refusal. Nothing
synthesized here is ever executed; every check that uses this stops at a
refusal the server must issue before running a handler.
"""

from collections.abc import Callable
from typing import Any

# Takes the resource_ref annotation ({"kind": ..., "enumerated_by": ...}) and
# returns a real item URI drawn from the live resource index.
ReferenceResolver = Callable[[dict[str, Any]], str]


class CannotSynthesize(Exception):
    """The schema needs something this generator cannot invent."""


def minimal_params(
    schema: dict[str, Any], resolve_ref: "ReferenceResolver | None" = None
) -> dict[str, Any]:
    """Schema-valid parameters covering only the required properties.

    ``resolve_ref`` supplies a real item URI for ``resource_ref`` properties
    (looked up from the live resource index); without one, any
    ``resource_ref`` requirement raises :class:`CannotSynthesize`.

    Example:
        >>> minimal_params({"type": "object", "required": ["n"],
        ...                 "properties": {"n": {"type": "number", "minimum": 2}}})
        {'n': 2}
    """
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    params: dict[str, Any] = {}
    for name in required:
        prop = properties.get(name)
        if not isinstance(prop, dict):
            raise CannotSynthesize(f"required property {name!r} has no schema")
        params[name] = _value_for(prop, resolve_ref)
    return params


def _value_for(prop: dict[str, Any], resolve_ref: "ReferenceResolver | None") -> Any:
    if "const" in prop:
        return prop["const"]
    if isinstance(prop.get("enum"), list) and prop["enum"]:
        return prop["enum"][0]
    if "resource_ref" in prop:
        if resolve_ref is None:
            raise CannotSynthesize("resource_ref property with no live index to draw from")
        return resolve_ref(prop["resource_ref"])
    kind = prop.get("type")
    if isinstance(kind, list):
        kind = kind[0] if kind else None
    if kind == "number" or kind == "integer":
        low = prop.get("minimum", prop.get("exclusiveMinimum"))
        base = 1 if low is None else low
        if "exclusiveMinimum" in prop and base == prop["exclusiveMinimum"]:
            base = base + 1
        return int(base) if kind == "integer" else base
    if kind == "string":
        return "conformance"
    if kind == "boolean":
        return False
    if kind == "array":
        minimum = prop.get("minItems", 0)
        items = prop.get("items")
        if minimum == 0:
            return []
        if not isinstance(items, dict):
            raise CannotSynthesize("non-empty array with no items schema")
        return [_value_for(items, resolve_ref) for _ in range(minimum)]
    if kind == "object":
        return minimal_params(prop, resolve_ref)
    raise CannotSynthesize(f"cannot invent a value for schema {prop!r}")
