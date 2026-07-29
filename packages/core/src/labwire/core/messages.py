"""Protocol message params/result models and the method registry (SPEC §16).

``MESSAGE_TYPES`` maps every protocol method name to its params model and
(for requests) result model. The registry is what validates SPEC.md's
examples and what the MCP adapter (M5) uses as a method → schema table.

Example:
    >>> from labwire.core.messages import MESSAGE_TYPES
    >>> MESSAGE_TYPES["ping"].result is not None
    True
"""

from typing import Any, Literal, NamedTuple

from labwire.core.capabilities import InstrumentDescriptor
from labwire.core.types import JsonRpcError
from pydantic import BaseModel, ConfigDict

CommandState = Literal["accepted", "running", "canceling", "succeeded", "failed", "canceled"]
TERMINAL_STATES: frozenset[str] = frozenset({"succeeded", "failed", "canceled"})

EventSeverity = Literal["info", "warning", "alarm"]


class _Msg(BaseModel):
    """Base for message payloads: unknown fields tolerated and preserved."""

    model_config = ConfigDict(extra="allow")


class PeerInfo(_Msg):
    """``{name, version}`` identifying one side's software (SPEC §6.1)."""

    name: str
    version: str


class ClientCapabilities(_Msg):
    """Client capability flags; reserved (empty) in v0.1 (SPEC §6.1)."""


class ServerCapabilities(_Msg):
    """Server capability flags (SPEC §6.1); absent flags default to false."""

    telemetry: bool = False
    events: bool = False
    manifests: bool = False
    resources: bool = False
    grants: bool = False


class InitializeParams(_Msg):
    """Params of ``initialize`` (SPEC §6.1)."""

    protocol_version: str
    client_info: PeerInfo
    capabilities: ClientCapabilities
    api_key: str | None = None


class InitializeResult(_Msg):
    """Result of ``initialize`` (SPEC §6.1)."""

    protocol_version: str
    server_info: PeerInfo
    capabilities: ServerCapabilities


class EmptyParams(_Msg):
    """Empty params object."""


class EmptyResult(_Msg):
    """Empty result object."""


class Authorization(_Msg):
    """An operator grant presented on an S3 submission (SPEC §8.6).

    An object rather than a bare string so a future cryptographic scheme
    can add members without changing the field's type.
    """

    grant_id: str


class SubmitParams(_Msg):
    """Params of ``command/submit`` (SPEC §8.2)."""

    command: str
    params: dict[str, Any]
    confirmation: str | None = None
    authorization: Authorization | None = None
    if_revision: dict[str, str] | None = None
    """Maps resource URI to the revision the client planned against (SPEC §10.5)."""


class SubmitResult(_Msg):
    """Result of ``command/submit``: the run was accepted (SPEC §8.2)."""

    command_id: str
    status: CommandState


class Progress(_Msg):
    """Optional progress payload on running status (SPEC §8.2)."""

    fraction: float | None = None
    message: str | None = None


class CancellationBoundary(_Msg):
    """The last completed step of a between_steps cancellation (SPEC §8.3)."""

    completed_steps: int
    of_steps: int | None = None
    last: str


class Cancellation(_Msg):
    """Settlement of an accepted cancel: what actually happened (SPEC §8.3)."""

    requested_at: str | None = None
    outcome: Literal[
        "never_started", "halted", "halted_at_boundary", "ran_to_completion", "unconfirmed"
    ]
    boundary: CancellationBoundary | None = None
    detail: str | None = None


class ResourceRevision(_Msg):
    """One resource's revision after a run changed it (SPEC §8.2)."""

    uri: str
    revision: str


class CommandStatus(_Msg):
    """The CommandStatus object (SPEC §8.2): pushed and polled alike."""

    command_id: str
    status: CommandState
    progress: Progress | None = None
    result: Any = None
    error: JsonRpcError | None = None
    cancellation: Cancellation | None = None
    resource_revisions: list[ResourceRevision] | None = None


class CommandIdParams(_Msg):
    """Params of ``command/status`` and ``command/cancel``."""

    command_id: str


class SubscribeParams(_Msg):
    """Params of ``telemetry/subscribe`` (SPEC §9.1)."""

    channels: list[str]
    max_rate_hz: float | None = None


class SubscribeResult(_Msg):
    """Result of ``telemetry/subscribe`` (SPEC §9.1)."""

    subscription_id: str


class UnsubscribeParams(_Msg):
    """Params of ``telemetry/unsubscribe`` (SPEC §9.1)."""

    subscription_id: str


class TelemetryNotification(_Msg):
    """Params of ``notifications/telemetry`` (SPEC §9.2)."""

    subscription_id: str
    channel: str
    seq: int
    timestamp: str
    value: Any = None


class EventNotification(_Msg):
    """Params of ``notifications/event`` (SPEC §11)."""

    name: str
    timestamp: str
    severity: EventSeverity
    data: dict[str, Any]


class ResourceReadParams(_Msg):
    """Params of ``resource/read`` (SPEC §10.2)."""

    uri: str


class ResourceIndexChildren(_Msg):
    """The items of one index entry: ids composing ``<entry-uri>/<id>`` (SPEC §10.2)."""

    kinds: list[str]
    ids: list[str]


class ResourceIndexEntry(_Msg):
    """One entry of a resource's reference index (SPEC §10.2)."""

    uri: str
    kinds: list[str]
    title: str | None = None
    children: ResourceIndexChildren | None = None


class ResourceReadResult(_Msg):
    """Result of ``resource/read`` (SPEC §10.2)."""

    uri: str
    kind: str
    revision: str
    read_at: str
    index_complete: bool
    index: list[ResourceIndexEntry]
    content: Any = None


class ResourceChangedData(_Msg):
    """``data`` of the reserved ``resource/changed`` event (SPEC §10.3)."""

    uri: str
    revision: str


class MessageTypes(NamedTuple):
    """Registry entry: params model, and result model for requests."""

    params: type[BaseModel]
    result: type[BaseModel] | None


MESSAGE_TYPES: dict[str, MessageTypes] = {
    "initialize": MessageTypes(InitializeParams, InitializeResult),
    "ping": MessageTypes(EmptyParams, EmptyResult),
    "instrument/describe": MessageTypes(EmptyParams, InstrumentDescriptor),
    "resource/read": MessageTypes(ResourceReadParams, ResourceReadResult),
    "command/submit": MessageTypes(SubmitParams, SubmitResult),
    "command/status": MessageTypes(CommandIdParams, CommandStatus),
    "command/cancel": MessageTypes(CommandIdParams, CommandStatus),
    "telemetry/subscribe": MessageTypes(SubscribeParams, SubscribeResult),
    "telemetry/unsubscribe": MessageTypes(UnsubscribeParams, EmptyResult),
    "notifications/initialized": MessageTypes(EmptyParams, None),
    "notifications/command_status": MessageTypes(CommandStatus, None),
    "notifications/telemetry": MessageTypes(TelemetryNotification, None),
    "notifications/event": MessageTypes(EventNotification, None),
}
"""Every protocol method (SPEC §16), for validation and schema export."""
