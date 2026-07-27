"""Labwire domain errors (SPEC §12).

Every error carries a JSON-RPC code, a category string, and a ``retryable``
flag agents key retry policy off.

Example:
    >>> from labwire.core.errors import BusyError
    >>> BusyError("all slots in use").retryable
    True
"""

from typing import Any, ClassVar, Self

from labwire.core.types import ErrorData, JsonRpcError


class LabwireError(Exception):
    """Base class for all Labwire protocol errors.

    Example:
        >>> err = LabwireError("boom")
        >>> err.code, err.category, err.retryable
        (-32008, 'internal', False)
    """

    code: ClassVar[int] = -32008
    category: ClassVar[str] = "internal"
    default_retryable: ClassVar[bool] = False

    _registry: ClassVar[dict[int, type["LabwireError"]]] = {}

    def __init__(
        self,
        message: str,
        *,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = self.default_retryable if retryable is None else retryable
        self.details = details

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._registry.setdefault(cls.code, cls)

    def to_wire(self) -> JsonRpcError:
        """Serialize to the SPEC §12.2 error object.

        Example:
            >>> BusyError("full").to_wire().data.category
            'busy'
        """
        return JsonRpcError(
            code=self.code,
            message=self.message,
            data=ErrorData(category=self.category, retryable=self.retryable, details=self.details),
        )

    @classmethod
    def _from_wire(cls, wire: JsonRpcError) -> Self:
        err = cls(
            wire.message,
            retryable=wire.data.retryable if wire.data else False,
            details=wire.data.details if wire.data else None,
        )
        return err


class ValidationError(LabwireError):
    """Params violate a schema, unknown entity, or rejected credential."""

    code = -32000
    category = "validation"


class UnsupportedError(LabwireError):
    """Command not declared, or method behind a capability advertised false."""

    code = -32001
    category = "unsupported"


class BusyError(LabwireError):
    """At command capacity (retryable) or session not initialized."""

    code = -32002
    category = "busy"
    default_retryable = True


class InterlockError(LabwireError):
    """Rejected or aborted because a declared interlock is tripped."""

    code = -32003
    category = "interlock"


class HardwareFaultError(LabwireError):
    """The instrument reported a hardware failure."""

    code = -32004
    category = "hardware_fault"


class DeviceTimeoutError(LabwireError):
    """The instrument did not respond internally in time."""

    code = -32005
    category = "timeout"
    default_retryable = True


class CanceledError(LabwireError):
    """The run was canceled."""

    code = -32006
    category = "canceled"


class NotCancelableError(LabwireError):
    """Cancel requested for a known run that cannot be canceled."""

    code = -32007
    category = "not_cancelable"


class InternalError(LabwireError):
    """Unexpected server error; never carries internal detail on the wire."""

    code = -32008
    category = "internal"


class ConfirmationRequiredError(LabwireError):
    """An S2 command was submitted without an acceptable confirmation."""

    code = -32009
    category = "confirmation_required"


class UnknownReferenceError(LabwireError):
    """A resource_ref value, or a resource/read URI, does not resolve (SPEC §10.4)."""

    code = -32010
    category = "unknown_reference"


class AuthorizationRequiredError(LabwireError):
    """An S3 command was submitted without a verifiable operator grant (SPEC §8.6)."""

    code = -32011
    category = "authorization_required"


class StaleRevisionError(LabwireError):
    """An if_revision precondition did not match the current revision (SPEC §10.5)."""

    code = -32012
    category = "stale_revision"


class InvalidRequestError(LabwireError):
    """JSON-RPC -32600: structurally invalid request (e.g. duplicate initialize)."""

    code = -32600
    category = "protocol"


class MethodNotFoundError(LabwireError):
    """JSON-RPC -32601: the request names a method this party does not implement."""

    code = -32601
    category = "protocol"


class InvalidParamsError(LabwireError):
    """JSON-RPC -32602: params malformed for the method (e.g. missing command_id)."""

    code = -32602
    category = "protocol"


def error_from_wire(wire: JsonRpcError) -> LabwireError:
    """Reconstruct the typed error for a wire error object.

    Unknown codes fall back to :class:`LabwireError`; per SPEC §12.2, errors
    lacking ``data.retryable`` are treated as not retryable.

    Example:
        >>> error_from_wire(BusyError("full").to_wire()).retryable
        True
    """
    cls = LabwireError._registry.get(wire.code)  # pyright: ignore[reportPrivateUsage]
    if cls is not None:
        return cls._from_wire(wire)  # pyright: ignore[reportPrivateUsage]
    err = LabwireError._from_wire(wire)  # pyright: ignore[reportPrivateUsage]
    err.__dict__["code"] = wire.code
    return err
