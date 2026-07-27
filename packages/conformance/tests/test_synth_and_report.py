"""Unit tests for parameter synthesis and verdict arithmetic."""

import pytest
from labwire.conformance import CheckOutcome, Report, Status
from labwire.conformance._synth import CannotSynthesize, minimal_params


def _outcome(check_id: str, level: str, status: Status) -> CheckOutcome:
    return CheckOutcome(check_id, "SPEC x", level, status, "why")


def test_minimal_params_covers_required_only() -> None:
    schema = {
        "type": "object",
        "required": ["volume", "channel"],
        "properties": {
            "volume": {"type": "number", "minimum": 0.5},
            "channel": {"type": "integer"},
            "note": {"type": "string"},
        },
    }
    assert minimal_params(schema) == {"volume": 0.5, "channel": 1}


def test_minimal_params_respects_enum_const_and_arrays() -> None:
    schema = {
        "type": "object",
        "required": ["mode", "flag", "wells"],
        "properties": {
            "mode": {"enum": ["fast", "slow"]},
            "flag": {"const": True},
            "wells": {"type": "array", "minItems": 2, "items": {"type": "string"}},
        },
    }
    assert minimal_params(schema) == {
        "mode": "fast",
        "flag": True,
        "wells": ["conformance", "conformance"],
    }


def test_minimal_params_resolves_resource_refs_via_callback() -> None:
    schema = {
        "type": "object",
        "required": ["slot"],
        "properties": {"slot": {"type": "string", "resource_ref": {"kind": "site"}}},
    }
    params = minimal_params(schema, lambda annotation: f"labwire:rack/{annotation['kind']}-1")
    assert params == {"slot": "labwire:rack/site-1"}


def test_minimal_params_refuses_resource_refs_without_an_index() -> None:
    schema = {
        "type": "object",
        "required": ["slot"],
        "properties": {"slot": {"resource_ref": {"kind": "site"}}},
    }
    with pytest.raises(CannotSynthesize):
        minimal_params(schema)


def test_verdict_ladder_stops_at_the_first_broken_level() -> None:
    report = Report(instrument="X", target="ws://x")
    report.add(_outcome("core.a", "core", Status.PASSED))
    report.add(_outcome("streaming.b", "streaming", Status.FAILED))
    report.add(_outcome("signed.c", "signed", Status.PASSED))
    level, blockers = report.verdict()
    assert level == "core"
    assert blockers
    assert "streaming.b" in blockers[0]


def test_not_applicable_never_blocks_but_unexercised_does() -> None:
    report = Report(instrument="X", target="ws://x")
    report.add(_outcome("core.a", "core", Status.PASSED))
    report.add(_outcome("core.na", "core", Status.NOT_APPLICABLE))
    assert report.verdict() == ("signed", [])
    report.add(_outcome("core.skip", "core", Status.UNEXERCISED))
    level, blockers = report.verdict()
    assert level == "none"
    assert "core.skip" in blockers[0]
