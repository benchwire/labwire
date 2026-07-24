"""Tests for RFC 8785 (JCS) canonicalization (SPEC §12.2)."""

import math

import pytest
from labwire.core.jcs import jcs_dumps


def test_number_formatting_follows_ecmascript_rules() -> None:
    cases = [
        ({"v": 1.0}, '{"v":1}'),
        ({"v": -0.0}, '{"v":0}'),
        ({"v": 0}, '{"v":0}'),
        ({"v": 25.5}, '{"v":25.5}'),
        ({"v": 0.1}, '{"v":0.1}'),
        ({"v": 1e16}, '{"v":10000000000000000}'),
        ({"v": 1e20}, '{"v":100000000000000000000}'),
        ({"v": 1e21}, '{"v":1e+21}'),
        ({"v": 5e-7}, '{"v":5e-7}'),
        ({"v": 0.000001}, '{"v":0.000001}'),
        ({"v": 1.5e-5}, '{"v":0.000015}'),
        ({"v": 9007199254740992.0}, '{"v":9007199254740992}'),
    ]
    for value, expected in cases:
        assert jcs_dumps(value) == expected, value


def test_strings_use_minimal_escaping_with_literal_utf8() -> None:
    assert jcs_dumps({"v": "25°C"}) == '{"v":"25°C"}'
    assert jcs_dumps({"v": 'say "hi"\n'}) == '{"v":"say \\"hi\\"\\n"}'


def test_keys_sort_and_values_nest() -> None:
    doc = {"b": [1.5, "x", None, True], "a": {"z": 1, "y": {"n": 2}}}
    assert jcs_dumps(doc) == '{"a":{"y":{"n":2},"z":1},"b":[1.5,"x",null,true]}'


def test_non_finite_numbers_are_rejected() -> None:
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="non-finite"):
            jcs_dumps({"v": bad})


def test_non_json_types_are_rejected() -> None:
    with pytest.raises(TypeError, match="not JSON-serializable"):
        jcs_dumps({"v": object()})
