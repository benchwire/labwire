"""Tests for protocol message models and the method registry."""

from labwire.core.capabilities import (
    ChannelSpec,
    CommandSpec,
    IdentityInfo,
    InstrumentDescriptor,
    InterlockSpec,
)
from labwire.core.messages import (
    MESSAGE_TYPES,
    CommandStatus,
    InitializeResult,
    ServerCapabilities,
)


def test_registry_covers_every_spec_method() -> None:
    expected = {
        "initialize",
        "ping",
        "instrument/describe",
        "resource/read",
        "command/submit",
        "command/status",
        "command/cancel",
        "telemetry/subscribe",
        "telemetry/unsubscribe",
        "notifications/initialized",
        "notifications/command_status",
        "notifications/telemetry",
        "notifications/event",
    }
    assert set(MESSAGE_TYPES) == expected


def test_notifications_have_no_result_model() -> None:
    for method, entry in MESSAGE_TYPES.items():
        if method.startswith("notifications/"):
            assert entry.result is None, method
        else:
            assert entry.result is not None, method


def test_capability_flags_default_false() -> None:
    caps = ServerCapabilities()
    assert (caps.telemetry, caps.events, caps.manifests) == (False, False, False)


def test_initialize_result_shape() -> None:
    result = InitializeResult.model_validate(
        {
            "protocol_version": "0.1",
            "server_info": {"name": "s", "version": "1"},
            "capabilities": {"telemetry": True},
        }
    )
    assert result.capabilities.telemetry is True
    assert result.capabilities.events is False


def test_command_status_error_is_typed() -> None:
    status = CommandStatus.model_validate(
        {
            "command_id": "x",
            "status": "failed",
            "error": {
                "code": -32003,
                "message": "tripped",
                "data": {"category": "interlock", "retryable": False},
            },
        }
    )
    assert status.error is not None
    assert status.error.data is not None
    assert status.error.data.category == "interlock"


def test_descriptor_composes_and_defaults() -> None:
    desc = InstrumentDescriptor(
        identity=IdentityInfo(manufacturer="m", model="d", serial_number="s", firmware_version="1"),
        commands=[
            CommandSpec(
                name="go",
                title="Go",
                description="Run.",
                params_schema={"type": "object", "additionalProperties": False},
                interruptible=False,
            )
        ],
        channels=[ChannelSpec(name="c", description="chan", dtype="float64", unit="g")],
        interlocks=[InterlockSpec(name="i", description="lock", kind="hard", tripped=False)],
    )
    assert desc.max_concurrent_commands == 1
    assert desc.commands[0].clears_interlocks == []
    assert desc.commands[0].unit_annotations == {}
