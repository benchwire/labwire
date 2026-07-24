"""Tests for the Labwire error hierarchy."""

import pytest
from labwire.core.errors import (
    BusyError,
    CanceledError,
    HardwareFaultError,
    InterlockError,
    InternalError,
    LabwireError,
    NotCancelableError,
    UnsupportedError,
    ValidationError,
    error_from_wire,
)
from labwire.core.types import JsonRpcError


def test_every_error_carries_code_category_and_default_retryable() -> None:
    cases = [
        (ValidationError, -32000, "validation", False),
        (UnsupportedError, -32001, "unsupported", False),
        (BusyError, -32002, "busy", True),
        (InterlockError, -32003, "interlock", False),
        (HardwareFaultError, -32004, "hardware_fault", False),
        (CanceledError, -32006, "canceled", False),
        (NotCancelableError, -32007, "not_cancelable", False),
        (InternalError, -32008, "internal", False),
    ]
    for cls, code, category, retryable in cases:
        err = cls("boom")
        assert err.code == code, cls.__name__
        assert err.category == category, cls.__name__
        assert err.retryable is retryable, cls.__name__


def test_retryable_can_be_overridden_per_instance() -> None:
    err = HardwareFaultError("transient glitch", retryable=True)
    assert err.retryable is True


def test_to_wire_produces_spec_error_object() -> None:
    wire = BusyError("1 of 1 slots in use").to_wire()
    assert isinstance(wire, JsonRpcError)
    assert wire.code == -32002
    assert wire.message == "1 of 1 slots in use"
    assert wire.data is not None
    assert wire.data.category == "busy"
    assert wire.data.retryable is True


def test_details_pass_through_to_wire() -> None:
    wire = BusyError("full", details={"retry_after_s": 2.5}).to_wire()
    assert wire.data is not None
    assert wire.data.details == {"retry_after_s": 2.5}


def test_error_from_wire_reconstructs_typed_error() -> None:
    wire = InterlockError("over_pressure tripped").to_wire()
    err = error_from_wire(wire)
    assert isinstance(err, InterlockError)
    assert err.retryable is False
    assert "over_pressure" in str(err)


def test_error_from_wire_unknown_code_falls_back_to_base() -> None:
    wire = JsonRpcError.model_validate({"code": -31999, "message": "vendor-specific"})
    err = error_from_wire(wire)
    assert type(err) is LabwireError
    assert err.code == -31999
    assert err.retryable is False  # errors lacking data.retryable are not retryable


def test_labwire_error_is_an_exception() -> None:
    with pytest.raises(LabwireError):
        raise ValidationError("bad params")
