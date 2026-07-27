"""Every quantity carries a UCUM code, including inside a container.

Protocol v0.2 promised that no quantity crosses the wire without a unit, and
for a while that was false: the check looked at properties whose JSON Schema
``type`` was ``number`` or ``integer``, and an array of numbers has type
``array``. A bare ``float`` parameter was refused at declaration time while
``list[float]`` was accepted silently, which mattered because an entire domain
(liquid handling, where every volume is an eight-channel array) passes its
quantities that way. See SPEC-FINDINGS.md, finding F5.

These tests hold both layers of the guarantee: declaration time, where the
audience is a driver author, and wire validation, where the audience is a
client refusing to trust an under-annotated instrument.
"""

from typing import Any

import pytest
from labwire.core.capabilities import (
    ChannelSpec,
    CommandSpec,
    IdentityInfo,
    InstrumentDescriptor,
    carries_number,
    numeric_property_names,
)
from labwire.core.server import CommandContext, command
from pydantic import BaseModel, ConfigDict, ValidationError

# --- layer 1: declaration time ----------------------------------------------


def _declare(annotation: Any, units: dict[str, str] | None = None) -> None:
    """Declare a command with one parameter of the given type."""

    @command(units=units or {})
    async def pour(  # pyright: ignore[reportUnusedFunction]
        self: Any,
        ctx: CommandContext,
        quantity: annotation,  # pyright: ignore[reportInvalidTypeForm, reportGeneralTypeIssues, reportUnknownParameterType]
    ) -> None:
        """Pour something."""


@pytest.mark.parametrize(
    ("annotation", "label"),
    [
        (float, "a bare float"),
        (int, "a bare int"),
        (list[float], "an array of floats"),
        (list[int], "an array of ints"),
        (list[float] | None, "an optional array"),
        (list[list[float]], "a nested array"),
        (tuple[float, float], "a fixed-length tuple"),
    ],
)
def test_a_quantity_without_a_unit_is_refused_at_declaration(annotation: Any, label: str) -> None:
    with pytest.raises(TypeError, match="lack UCUM unit codes"):
        _declare(annotation)
    assert label  # the label documents the case in the test report


@pytest.mark.parametrize(
    "annotation",
    [float, list[float], list[float] | None, list[list[float]], tuple[float, float]],
)
def test_the_same_declaration_is_accepted_once_annotated(annotation: Any) -> None:
    _declare(annotation, {"quantity": "uL"})  # does not raise


@pytest.mark.parametrize(
    "annotation",
    [str, list[str], bool, list[bool], list[list[str]], dict[str, str], None],
)
def test_things_that_are_not_quantities_need_no_unit(annotation: Any) -> None:
    _declare(annotation)  # does not raise


@pytest.mark.parametrize("annotation", [dict[str, float], list[dict[str, float]]])
def test_a_mapping_of_quantities_is_refused_however_it_is_annotated(annotation: Any) -> None:
    """One code cannot describe quantities under names the schema never states.

    The named form of the same bundle is already refused with "flatten them";
    withholding the field names must not remove the objection. Found by the
    audit: nine of fourteen shipped driver commands used this shape.
    """
    with pytest.raises(TypeError, match="does not declare"):
        _declare(annotation)
    with pytest.raises(TypeError, match="does not declare"):
        _declare(annotation, {"quantity": "Cel"})  # a plausible code does not help


def test_the_error_says_a_unit_is_needed_inside_a_list_too() -> None:
    """The message has to teach the rule, since the author is the audience."""
    with pytest.raises(TypeError, match="including inside a list"):
        _declare(list[float])


def test_dimensionless_counts_are_declared_rather_than_waived() -> None:
    """An index has no dimension, and "1" is how UCUM says so out loud."""
    _declare(list[int], {"quantity": "1"})  # does not raise
    with pytest.raises(TypeError, match="lack UCUM unit codes"):
        _declare(list[int], {"quantity": "   "})  # whitespace is not a code


# --- layer 2: wire validation ----------------------------------------------


def _wire_command(properties: dict[str, Any], units: dict[str, str]) -> dict[str, Any]:
    return {
        "name": "pour",
        "title": "Pour",
        "description": "Pour something.",
        "params_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
        },
        "unit_annotations": units,
        "interruptible": False,
    }


def _wire_descriptor(command_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity": {
            "manufacturer": "m",
            "model": "d",
            "serial_number": "s",
            "firmware_version": "1",
        },
        "commands": [command_payload],
        "channels": [],
        "interlocks": [],
    }


ARRAY_OF_NUMBERS = {"type": "array", "items": {"type": "number"}}


def test_a_descriptor_with_an_unannotated_array_is_refused_on_receipt() -> None:
    """What a client does when an instrument under-annotates itself."""
    payload = _wire_descriptor(_wire_command({"volumes_ul": ARRAY_OF_NUMBERS}, {}))
    with pytest.raises(ValidationError, match="volumes_ul"):
        InstrumentDescriptor.model_validate(payload)


def test_the_same_descriptor_is_accepted_when_annotated() -> None:
    payload = _wire_descriptor(
        _wire_command({"volumes_ul": ARRAY_OF_NUMBERS}, {"volumes_ul": "uL"})
    )
    descriptor = InstrumentDescriptor.model_validate(payload)
    assert descriptor.commands[0].unit_annotations["volumes_ul"] == "uL"


def test_a_nested_array_is_refused_on_receipt() -> None:
    nested = {"type": "array", "items": ARRAY_OF_NUMBERS}
    with pytest.raises(ValidationError, match="grid"):
        InstrumentDescriptor.model_validate(_wire_descriptor(_wire_command({"grid": nested}, {})))


def test_an_optional_array_written_as_anyof_is_refused_on_receipt() -> None:
    """pydantic writes `list[float] | None` this way, so the check must see it."""
    optional = {"anyOf": [ARRAY_OF_NUMBERS, {"type": "null"}]}
    with pytest.raises(ValidationError, match="flow_rates"):
        InstrumentDescriptor.model_validate(
            _wire_descriptor(_wire_command({"flow_rates": optional}, {}))
        )


def test_a_reference_into_defs_is_followed() -> None:
    """A model-typed parameter hides behind a $ref; the check has to follow it."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"readings": {"$ref": "#/$defs/Readings"}},
        "$defs": {"Readings": ARRAY_OF_NUMBERS},
    }
    with pytest.raises(ValidationError, match="readings"):
        CommandSpec.model_validate(
            {
                "name": "read",
                "title": "Read",
                "description": "Read.",
                "params_schema": schema,
                "interruptible": False,
            }
        )


# --- results ----------------------------------------------------------------


def test_an_array_result_needs_returns_units() -> None:
    with pytest.raises(TypeError, match="name no field"):

        @command()
        async def measure(  # pyright: ignore[reportUnusedFunction]
            self: Any, ctx: CommandContext
        ) -> list[float]:
            """Measure."""
            return []


class Readings(BaseModel):
    """A result model whose quantity arrives as an array."""

    model_config = ConfigDict(extra="forbid")

    volumes_ul: list[float]
    wells: list[str]


def test_a_named_array_result_field_needs_returns_units() -> None:
    with pytest.raises(TypeError, match="volumes_ul"):

        @command()
        async def measure(  # pyright: ignore[reportUnusedFunction]
            self: Any, ctx: CommandContext
        ) -> Readings:
            """Measure."""
            raise NotImplementedError


def test_the_same_result_model_is_accepted_once_annotated() -> None:
    @command(returns_units={"volumes_ul": "uL"})
    async def measure(  # pyright: ignore[reportUnusedFunction]
        self: Any, ctx: CommandContext
    ) -> Readings:
        """Measure."""
        raise NotImplementedError


# --- telemetry --------------------------------------------------------------


def test_a_channel_always_needs_a_unit_so_it_never_had_this_hole() -> None:
    """Channels carry scalars and require a unit unconditionally.

    Recorded as a test rather than a claim: the F5 audit covered telemetry
    too, and the reason nothing changed there is that ``ChannelSpec`` demands
    a unit for every channel regardless of dtype, and v0.2 channel dtypes are
    scalar only.
    """
    assert set(ChannelSpec.model_fields) >= {"unit", "dtype"}
    with pytest.raises(ValidationError, match="non-empty UCUM code"):
        ChannelSpec(name="mass", description="d", dtype="float64", unit="  ")
    for dtype in ("float64", "int64", "bool", "string"):
        with pytest.raises(ValidationError):
            ChannelSpec(name="c", description="d", dtype=dtype, unit="")  # pyright: ignore[reportArgumentType]


# --- nested objects: refused rather than silently unannotated ---------------


def test_an_object_parameter_with_numeric_fields_is_refused_with_advice() -> None:
    """v0.2 keys units by parameter, so a nested object cannot be annotated.

    Letting it through would reopen the hole one level down. Refusing it names
    the fields and says what to do instead.
    """
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"setpoint": {"$ref": "#/$defs/Setpoint"}},
        "$defs": {
            "Setpoint": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "temperature_c": {"type": "number"},
                    "pressure_kpa": {"type": "number"},
                    "label": {"type": "string"},
                },
            }
        },
    }
    with pytest.raises(ValidationError) as caught:
        CommandSpec.model_validate(
            {
                "name": "apply",
                "title": "Apply",
                "description": "Apply.",
                "params_schema": schema,
                "interruptible": False,
            }
        )
    message = str(caught.value)
    assert "setpoint.temperature_c" in message  # the path, not just the name
    assert "setpoint.pressure_kpa" in message
    assert "label" not in message  # only the quantities are the problem
    assert "Flatten" in message


# --- the primitive ----------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": "number"}, True),
        ({"type": "integer"}, True),
        ({"type": "string"}, False),
        ({"type": "array", "items": {"type": "number"}}, True),
        ({"type": "array", "items": {"type": "string"}}, False),
        ({"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}, True),
        ({"anyOf": [{"type": "number"}, {"type": "null"}]}, True),
        ({"anyOf": [{"type": "string"}, {"type": "null"}]}, False),
        ({"prefixItems": [{"type": "string"}, {"type": "number"}]}, True),
        ({"additionalProperties": {"type": "number"}}, True),
        # An unconstrained schema permits anything, a quantity included, so it
        # counts. Failing open here is what let the shipped bridge leak.
        ({}, True),
        # A descriptor arriving over the wire was not necessarily written by
        # pydantic, so the other legal ways to say "number" have to count too.
        ({"type": ["number", "null"]}, True),
        ({"type": ["string", "null"]}, False),
        ({"const": 42}, True),
        ({"const": "left"}, False),
        ({"const": True}, False),  # a bool is not a quantity
        ({"enum": [1, 2, 5]}, True),
        ({"enum": ["low", "high"]}, False),
        ({"enum": [True, False]}, False),
        ({"type": "array", "items": {"enum": [0.1, 0.2]}}, True),
        ({"allOf": [{"type": "number"}]}, True),
    ],
)
def test_carries_number_looks_through_containers(schema: dict[str, Any], expected: bool) -> None:
    assert carries_number(schema, {}) is expected


def test_numeric_property_names_reports_only_the_quantities() -> None:
    schema = {
        "properties": {
            "volumes_ul": {"type": "array", "items": {"type": "number"}},
            "wells": {"type": "array", "items": {"type": "string"}},
            "rate": {"type": "number"},
            "label": {"type": "string"},
        }
    }
    assert numeric_property_names(schema) == {"volumes_ul", "rate"}


def test_a_pathological_self_referential_schema_terminates() -> None:
    """A cyclic $ref must not hang the validator."""
    schema = {"$ref": "#/$defs/Loop", "$defs": {"Loop": {"items": {"$ref": "#/$defs/Loop"}}}}
    assert carries_number(schema, schema) is False


def test_identity_is_unaffected() -> None:
    """A guard that the change stayed inside the unit rules."""
    assert (
        IdentityInfo(manufacturer="m", model="d", serial_number="s", firmware_version="1").model
        == "d"
    )


# --- regressions from the adversarial audit of the F5 fix -------------------
#
# Every case below was found by an audit that set out to break the guarantee
# after the first fix landed, and each was reproduced against a running server
# before being fixed. They are grouped here so a future change that reopens one
# fails loudly.


def _refuses(params: dict[str, Any], units: dict[str, str] | None = None) -> str:
    with pytest.raises(ValidationError) as caught:
        CommandSpec.model_validate(
            {
                "name": "pour",
                "title": "Pour",
                "description": "Pour.",
                "params_schema": params,
                "unit_annotations": units or {},
                "interruptible": False,
            }
        )
    return str(caught.value)


def _closed(properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": properties, **extra}


def test_an_open_mapping_cannot_be_vouched_for() -> None:
    """`dict[str, Any]` shipped millimetres from the PyLabRobot bridge."""
    assert "does not say" in _refuses({"type": "object", "additionalProperties": True})


def test_an_object_that_does_not_close_its_properties_is_refused() -> None:
    """An undeclared extra member can be a quantity."""
    assert "does not say" in _refuses({"type": "object", "properties": {}})


def test_an_array_that_does_not_declare_its_items_is_refused() -> None:
    assert "does not say" in _refuses(_closed({"points": {"type": "array"}}))


def test_a_model_inside_a_container_does_not_hide_its_fields() -> None:
    """`list[Model]` escaped while a bare `Model` was refused."""
    schema = _closed(
        {"readings": {"type": "array", "items": {"$ref": "#/$defs/R"}}},
        **{"$defs": {"R": _closed({"absorbance_au": {"type": "number"}})}},
    )
    assert "readings[].absorbance_au" in _refuses(schema)


def test_a_root_level_ref_does_not_hide_the_whole_property_set() -> None:
    """The entry points used to read `properties` without resolving."""
    schema = {
        "$ref": "#/$defs/Common",
        "$defs": {"Common": _closed({"volume_ul": {"type": "number"}})},
    }
    assert "volume_ul" in _refuses(schema)


def test_a_multi_hop_ref_chain_is_followed() -> None:
    schema = _closed(
        {"v": {"$ref": "#/$defs/A"}},
        **{"$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"type": "number"}}},
    )
    assert "'v'" in _refuses(schema)


def test_a_draft_07_definitions_pointer_is_followed() -> None:
    """Most non-Python schema generators emit `definitions`, not `$defs`."""
    schema = _closed(
        {"v": {"$ref": "#/definitions/N"}}, **{"definitions": {"N": {"type": "number"}}}
    )
    assert "'v'" in _refuses(schema)


def test_an_unresolvable_ref_fails_closed() -> None:
    """A reference this build cannot follow is one it cannot vouch for."""
    assert "does not say" in _refuses(_closed({"v": {"$ref": "#/$defs/Missing"}}))


def test_a_remote_ref_fails_closed() -> None:
    assert "does not say" in _refuses(_closed({"v": {"$ref": "https://example.test/s.json"}}))


def test_a_broken_ref_does_not_erase_a_sibling_type() -> None:
    """Siblings of `$ref` are legal in 2020-12 and must survive."""
    schema = _closed({"v": {"$ref": "#/$defs/Missing", "type": "number"}})
    assert "does not say" in _refuses(schema)


def test_pattern_properties_are_walked() -> None:
    """pydantic emits these for a dict with a constrained key type."""
    schema = _closed(
        {
            "rates": {
                "type": "object",
                "patternProperties": {"^c": {"type": "number"}},
                "additionalProperties": False,
            }
        }
    )
    assert "rates" in _refuses(schema)


@pytest.mark.parametrize("keyword", ["unevaluatedItems", "additionalItems", "contains"])
def test_other_array_keywords_are_walked(keyword: str) -> None:
    schema = _closed(
        {"v": {"type": "array", "items": {"type": "string"}, keyword: {"type": "number"}}}
    )
    assert "v[]" in _refuses(schema)


def test_the_draft_07_tuple_form_of_items_is_walked() -> None:
    """`items` as a list is the eight-channel volume tuple, spelled draft-07."""
    schema = _closed({"volumes": {"type": "array", "items": [{"type": "number"}] * 8}})
    assert "volumes" in _refuses(schema)


def test_a_cyclic_ref_terminates() -> None:
    schema = {
        "$ref": "#/$defs/Node",
        "$defs": {"Node": _closed({"child": {"$ref": "#/$defs/Node"}, "v": {"type": "number"}})},
    }
    assert "v" in _refuses(schema)


def test_an_if_then_else_branch_is_walked() -> None:
    schema = _closed(
        {"v": {"if": {"const": "a"}, "then": {"type": "number"}, "else": {"type": "string"}}}
    )
    assert "'v'" in _refuses(schema)


def test_a_covered_path_satisfies_its_descendants() -> None:
    """Annotating a container annotates the quantities inside it."""
    schema = _closed(
        {"readings": {"type": "array", "items": {"$ref": "#/$defs/R"}}},
        **{"$defs": {"R": _closed({"volume_ul": {"type": "number"}})}},
    )
    with pytest.raises(ValidationError):
        CommandSpec.model_validate(
            {
                "name": "r",
                "title": "R",
                "description": "R.",
                "params_schema": schema,
                "interruptible": False,
            }
        )  # nested params are refused outright; results are where paths apply


def test_result_paths_may_be_annotated_by_path() -> None:
    """A result is legitimately a tree, so its units are keyed by path."""
    returns = _closed(
        {"labware": {"type": "array", "items": {"$ref": "#/$defs/L"}}},
        **{
            "$defs": {"L": _closed({"location_mm": {"type": "array", "items": {"type": "number"}}})}
        },
    )
    spec = CommandSpec.model_validate(
        {
            "name": "describe",
            "title": "Describe",
            "description": "Describe.",
            "params_schema": {"type": "object", "additionalProperties": False},
            "returns_schema": returns,
            "returns_units": {"labware[].location_mm": "mm"},
            "interruptible": False,
        }
    )
    assert spec.returns_units["labware[].location_mm"] == "mm"


def test_an_unannotated_result_path_is_still_refused() -> None:
    returns = _closed(
        {"labware": {"type": "array", "items": {"$ref": "#/$defs/L"}}},
        **{
            "$defs": {"L": _closed({"location_mm": {"type": "array", "items": {"type": "number"}}})}
        },
    )
    with pytest.raises(ValidationError, match=r"labware\[\]\.location_mm"):
        CommandSpec.model_validate(
            {
                "name": "describe",
                "title": "Describe",
                "description": "Describe.",
                "params_schema": {"type": "object", "additionalProperties": False},
                "returns_schema": returns,
                "returns_units": {},
                "interruptible": False,
            }
        )
