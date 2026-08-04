"""Translate Synapse status codes and gRPC failures into the Labwire taxonomy.

This module imports nothing from ``science-synapse``: the enum numbers below
are copied from ``synapse.api.status_pb2`` so the pure layer stays importable
on a machine that has never heard of a neural interface. :func:`status_names`
is the reconciliation point, and a number this table does not know is reported
as the number rather than guessed at.

Example:
    >>> from labwire.bridges.synapse.errors import status_name
    >>> status_name(2)
    'kInvalidConfiguration'
"""

from labwire.core import (
    DeviceTimeoutError,
    HardwareFaultError,
    InternalError,
    LabwireError,
    UnsupportedError,
    ValidationError,
)

STATUS_CODES: dict[int, str] = {
    0: "kOk",
    1: "kUndefinedError",
    2: "kInvalidConfiguration",
    3: "kFailedPrecondition",
    4: "kUnimplemented",
    5: "kInternalError",
    6: "kPermissionDenied",
    7: "kQueryFailed",
}
"""``synapse.StatusCode``, verified against science-synapse 2.7.6."""

DEVICE_STATES: dict[int, str] = {
    0: "kUnknown",
    1: "kInitializing",
    2: "kStopped",
    3: "kRunning",
    4: "kError",
}
"""``synapse.DeviceState``, verified against science-synapse 2.7.6."""

OK = 0
"""``StatusCode.kOk``: the only code that is not a failure."""

_MAPPING: dict[int, type[LabwireError]] = {
    1: HardwareFaultError,  # kUndefinedError: no information, so assume the worst
    2: ValidationError,  # kInvalidConfiguration: the parameters produced a bad chain
    3: ValidationError,  # kFailedPrecondition: retrying unchanged will not help
    4: UnsupportedError,  # kUnimplemented: this build of the device lacks the RPC
    5: InternalError,  # kInternalError: a fault inside the device software
    6: ValidationError,  # kPermissionDenied: a rejected credential, per SPEC 12
    7: HardwareFaultError,  # kQueryFailed: the query reached the device and failed
}
"""Synapse status code to Labwire error class.

``kUndefinedError`` is the conservative default rather than something an
agent might retry, following the PyLabRobot bridge's precedent. It is also
the code the shipped simulator returns for every state-precondition failure
("Device is not running", "Failed to stop streaming"), which is a coarser
vocabulary than the enum offers; see SYNAPSE.md, strain 6.

``kPermissionDenied`` deliberately does NOT map to
:class:`~labwire.core.AuthorizationRequiredError`: that error means "this S3
command needs an operator grant", and a device-side permission refusal is a
different fact. Conflating them would let a device response look like a
Labwire authorization decision.
"""


def status_name(code: int) -> str:
    """Name a ``synapse.StatusCode`` number, or say plainly that it is unknown.

    Example:
        >>> status_name(0), status_name(99)
        ('kOk', 'unknown status code 99')
    """
    return STATUS_CODES.get(code, f"unknown status code {code}")


def device_state_name(state: int) -> str:
    """Name a ``synapse.DeviceState`` number, or say plainly that it is unknown.

    Example:
        >>> device_state_name(3), device_state_name(42)
        ('kRunning', 'unknown device state 42')
    """
    return DEVICE_STATES.get(state, f"unknown device state {state}")


def map_status(code: int, message: str, *, rpc: str) -> LabwireError:
    """Translate a non-OK ``synapse.Status`` into a Labwire error.

    ``rpc`` names the call so the agent learns which round trip failed;
    Synapse status messages alone do not say.

    Example:
        >>> type(map_status(2, "bad chain", rpc="Configure")).__name__
        'ValidationError'
    """
    cls = _MAPPING.get(code, HardwareFaultError)
    detail = message.strip() or "(the device sent no message)"
    return cls(
        f"Synapse {rpc} returned {status_name(code)}: {detail}",
        details={"synapse_status_code": status_name(code), "rpc": rpc},
    )


def no_response(rpc: str, captured: str | None) -> LabwireError:
    """The error for a client call that returned nothing at all.

    Every method of ``synapse.client.device.Device`` catches
    ``grpc.RpcError``, writes it to a logger, and returns ``None`` or
    ``False``; the ``*_with_status`` variants do this too. The bridge
    reinstalls the lost detail by capturing that logger
    (:class:`~labwire.bridges.synapse.client.ClientErrorCapture`), and
    reports a timeout rather than a hardware fault because "the instrument
    did not respond" is what actually happened.

    Example:
        >>> type(no_response("Info", None)).__name__
        'DeviceTimeoutError'
    """
    suffix = f": {captured}" if captured else " (the client logged no detail)"
    return DeviceTimeoutError(
        f"the Synapse client returned no response for {rpc}; it swallows "
        f"grpc.RpcError and logs it{suffix}",
        details={"rpc": rpc, "grpc_detail": captured},
    )


def map_rpc_error(exc: BaseException, *, rpc: str) -> LabwireError:
    """Translate a raw ``grpc.RpcError`` raised by the generated stub.

    The bridge issues ``Configure`` through the stub rather than the client
    wrapper (see :mod:`labwire.bridges.synapse.client`), so this path sees
    real gRPC failures instead of swallowed ones. ``UNAVAILABLE`` and
    ``DEADLINE_EXCEEDED`` are transport conditions worth retrying; anything
    else is reported as a hardware fault.

    Example:
        >>> type(map_rpc_error(RuntimeError("boom"), rpc="Configure")).__name__
        'HardwareFaultError'
    """
    if isinstance(exc, LabwireError):
        return exc
    code = getattr(exc, "code", None)
    name = ""
    if callable(code):
        try:
            name = str(getattr(code(), "name", "") or "")
        except Exception:
            name = ""
    details_fn = getattr(exc, "details", None)
    detail = ""
    if callable(details_fn):
        try:
            detail = str(details_fn() or "")
        except Exception:
            detail = ""
    detail = detail or str(exc) or type(exc).__name__
    cls: type[LabwireError] = (
        DeviceTimeoutError if name in {"UNAVAILABLE", "DEADLINE_EXCEEDED"} else HardwareFaultError
    )
    return cls(
        f"Synapse {rpc} failed over gRPC ({name or type(exc).__name__}): {detail}",
        details={"rpc": rpc, "grpc_code": name or None},
    )
