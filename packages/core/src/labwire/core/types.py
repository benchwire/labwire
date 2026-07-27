"""JSON-RPC 2.0 envelope models for the Labwire protocol.

All models tolerate and preserve unknown fields (forward compatibility,
SPEC §2).

Example:
    >>> from labwire.core.types import parse_message
    >>> msg = parse_message({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    >>> msg.method
    'ping'
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class _Envelope(BaseModel):
    """Base for all wire messages: extra fields are allowed and preserved."""

    model_config = ConfigDict(extra="allow")

    jsonrpc: Literal["2.0"] = "2.0"


class JsonRpcRequest(_Envelope):
    """A JSON-RPC request: carries an ``id`` and expects a response.

    Example:
        >>> JsonRpcRequest(id=1, method="ping", params={}).method
        'ping'
    """

    id: int
    method: str
    params: dict[str, Any] = {}


class JsonRpcNotification(_Envelope):
    """A JSON-RPC notification: no ``id``, no response expected.

    Example:
        >>> JsonRpcNotification(method="notifications/initialized", params={}).method
        'notifications/initialized'
    """

    method: str
    params: dict[str, Any] = {}


class JsonRpcResponse(_Envelope):
    """A successful JSON-RPC response carrying a ``result``.

    Example:
        >>> JsonRpcResponse(id=1, result={}).id
        1
    """

    id: int
    result: Any = None


class ErrorData(BaseModel):
    """The ``data`` member of a Labwire error (SPEC §12.2).

    Example:
        >>> ErrorData(category="busy", retryable=True).category
        'busy'
    """

    model_config = ConfigDict(extra="allow")

    category: str
    retryable: bool
    details: dict[str, Any] | None = None


class JsonRpcError(BaseModel):
    """The error object carried by error responses and CommandStatus.

    Example:
        >>> JsonRpcError(code=-32601, message="no such method").code
        -32601
    """

    model_config = ConfigDict(extra="allow")

    code: int
    message: str
    data: ErrorData | None = None


class JsonRpcErrorResponse(_Envelope):
    """A JSON-RPC error response.

    Example:
        >>> raw = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "?"}}
        >>> JsonRpcErrorResponse.model_validate(raw).error.code
        -32601
    """

    id: int | None
    error: JsonRpcError


type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse | JsonRpcErrorResponse


def parse_message(raw: dict[str, Any]) -> JsonRpcMessage:
    """Discriminate a raw JSON object into one of the four envelope shapes.

    Raises:
        ValueError: if the object is not a JSON-RPC 2.0 message.

    Example:
        >>> type(parse_message({"jsonrpc": "2.0", "id": 1, "result": {}})).__name__
        'JsonRpcResponse'
    """
    if "method" in raw:
        if "id" in raw:
            return JsonRpcRequest.model_validate(raw)
        return JsonRpcNotification.model_validate(raw)
    if "error" in raw:
        return JsonRpcErrorResponse.model_validate(raw)
    if "result" in raw:
        return JsonRpcResponse.model_validate(raw)
    raise ValueError(f"not a JSON-RPC 2.0 message: keys={sorted(raw)}")
