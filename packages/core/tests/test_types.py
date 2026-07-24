"""Tests for JSON-RPC envelope models."""

from typing import Any

import pytest
from labwire.core.types import (
    JsonRpcError,
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    parse_message,
)


def test_request_round_trip() -> None:
    raw: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
    req = JsonRpcRequest.model_validate(raw)
    assert req.id == 1
    assert req.method == "ping"
    assert req.model_dump(mode="json") == raw


def test_notification_has_no_id() -> None:
    raw: dict[str, Any] = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    note = JsonRpcNotification.model_validate(raw)
    assert note.method == "notifications/initialized"
    assert "id" not in note.model_dump(mode="json")


def test_extra_fields_are_tolerated_and_preserved() -> None:
    raw: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "ping",
        "params": {},
        "future_field": True,
    }
    req = JsonRpcRequest.model_validate(raw)
    assert req.model_dump(mode="json")["future_field"] is True


def test_error_response_round_trip() -> None:
    raw = {
        "jsonrpc": "2.0",
        "id": 9,
        "error": {
            "code": -32002,
            "message": "busy",
            "data": {"category": "busy", "retryable": True},
        },
    }
    resp = JsonRpcErrorResponse.model_validate(raw)
    assert resp.error.code == -32002
    assert resp.error.data is not None
    assert resp.error.data.retryable is True
    assert resp.model_dump(mode="json", exclude_none=True) == raw


def test_parse_message_discriminates_all_four_shapes() -> None:
    assert isinstance(
        parse_message({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}),
        JsonRpcRequest,
    )
    assert isinstance(
        parse_message({"jsonrpc": "2.0", "method": "notifications/event", "params": {}}),
        JsonRpcNotification,
    )
    assert isinstance(parse_message({"jsonrpc": "2.0", "id": 1, "result": {}}), JsonRpcResponse)
    assert isinstance(
        parse_message(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no such method"}}
        ),
        JsonRpcErrorResponse,
    )


def test_parse_message_rejects_garbage() -> None:
    with pytest.raises(ValueError, match=r"not a JSON-RPC 2\.0 message"):
        parse_message({"hello": "world"})


def test_error_object_without_data_is_valid() -> None:
    err = JsonRpcError.model_validate({"code": -32700, "message": "parse error"})
    assert err.data is None
