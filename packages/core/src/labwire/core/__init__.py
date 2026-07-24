"""Labwire core protocol library: server and client SDKs.

Wrap any device in the protocol with :class:`Instrument` and
:class:`InstrumentServer`; drive any instrument with :class:`LabwireClient`.

Example:
    >>> from labwire.core import PROTOCOL_VERSION
    >>> PROTOCOL_VERSION
    '0.1'
"""

from labwire.core._meta import PROTOCOL_VERSION, __version__
from labwire.core.capabilities import (
    ChannelSpec,
    CommandSpec,
    IdentityInfo,
    InstrumentDescriptor,
    InterlockSpec,
)
from labwire.core.client import (
    CommandHandle,
    EventStream,
    LabwireClient,
    TelemetrySample,
    TelemetrySubscription,
)
from labwire.core.errors import (
    BusyError,
    CanceledError,
    DeviceTimeoutError,
    HardwareFaultError,
    InterlockError,
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    LabwireError,
    MethodNotFoundError,
    NotCancelableError,
    UnsupportedError,
    ValidationError,
    error_from_wire,
)
from labwire.core.jcs import jcs_canonical, jcs_dumps
from labwire.core.messages import (
    MESSAGE_TYPES,
    CommandState,
    CommandStatus,
    EventNotification,
    EventSeverity,
    PeerInfo,
    Progress,
    ServerCapabilities,
)
from labwire.core.server import (
    Clock,
    CommandContext,
    Instrument,
    InstrumentServer,
    Interlock,
    RunRecord,
    SystemClock,
    TelemetryChannel,
    channel,
    command,
    interlock,
)
from labwire.core.session import JsonRpcSession, SessionClosed
from labwire.core.signing import (
    MANIFEST_VERSION,
    Manifest,
    SigningKey,
    VerificationResult,
    sign_manifest,
    verify_bundle,
    verify_manifest,
)
from labwire.core.transport import MemoryTransport, Transport, TransportClosed, WebSocketTransport

__all__ = [
    "MANIFEST_VERSION",
    "MESSAGE_TYPES",
    "PROTOCOL_VERSION",
    "BusyError",
    "CanceledError",
    "ChannelSpec",
    "Clock",
    "CommandContext",
    "CommandHandle",
    "CommandSpec",
    "CommandState",
    "CommandStatus",
    "DeviceTimeoutError",
    "EventNotification",
    "EventSeverity",
    "EventStream",
    "HardwareFaultError",
    "IdentityInfo",
    "Instrument",
    "InstrumentDescriptor",
    "InstrumentServer",
    "Interlock",
    "InterlockError",
    "InterlockSpec",
    "InternalError",
    "InvalidParamsError",
    "InvalidRequestError",
    "JsonRpcSession",
    "LabwireClient",
    "LabwireError",
    "Manifest",
    "MemoryTransport",
    "MethodNotFoundError",
    "NotCancelableError",
    "PeerInfo",
    "Progress",
    "RunRecord",
    "ServerCapabilities",
    "SessionClosed",
    "SigningKey",
    "SystemClock",
    "TelemetryChannel",
    "TelemetrySample",
    "TelemetrySubscription",
    "Transport",
    "TransportClosed",
    "UnsupportedError",
    "ValidationError",
    "VerificationResult",
    "WebSocketTransport",
    "__version__",
    "channel",
    "command",
    "error_from_wire",
    "interlock",
    "jcs_canonical",
    "jcs_dumps",
    "sign_manifest",
    "verify_bundle",
    "verify_manifest",
]
