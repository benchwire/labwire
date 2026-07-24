"""Instrument capability description models (SPEC §7).

Example:
    >>> from labwire.core.capabilities import IdentityInfo
    >>> IdentityInfo(
    ...     manufacturer="Labwire Project",
    ...     model="SimPump-100",
    ...     serial_number="SIM-0001",
    ...     firmware_version="0.1.0",
    ... ).model
    'SimPump-100'
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class _SpecModel(BaseModel):
    """Base for descriptor models: unknown fields tolerated and preserved."""

    model_config = ConfigDict(extra="allow")


class IdentityInfo(_SpecModel):
    """Instrument identity (SPEC §7.1); embedded verbatim in manifests (§12).

    Example:
        >>> IdentityInfo(
        ...     manufacturer="m", model="d", serial_number="s", firmware_version="1"
        ... ).firmware_hash is None
        True
    """

    manufacturer: str
    model: str
    serial_number: str
    firmware_version: str
    firmware_hash: str | None = None


class CommandSpec(_SpecModel):
    """A declared command (SPEC §7.2).

    Example:
        >>> CommandSpec(
        ...     name="go",
        ...     title="Go",
        ...     description="Run.",
        ...     params_schema={"type": "object", "additionalProperties": False},
        ...     interruptible=False,
        ... ).interruptible
        False
    """

    name: str
    title: str
    description: str
    params_schema: dict[str, Any]
    unit_annotations: dict[str, str] = {}
    returns_schema: dict[str, Any] | None = None
    estimated_duration_s: float | None = None
    interruptible: bool
    clears_interlocks: list[str] = []


class ChannelSpec(_SpecModel):
    """A typed measurement channel (SPEC §7.3).

    Example:
        >>> ChannelSpec(name="mass", description="w", dtype="float64", unit="g").unit
        'g'
    """

    name: str
    description: str
    dtype: Literal["float64", "int64", "bool", "string"]
    unit: str
    sample_rate_hz_hint: float | None = None


class InterlockSpec(_SpecModel):
    """A declared safety interlock (SPEC §7.4).

    Example:
        >>> InterlockSpec(name="i", description="d", kind="hard", tripped=False).kind
        'hard'
    """

    name: str
    description: str
    kind: Literal["hard", "soft"]
    tripped: bool


class InstrumentDescriptor(_SpecModel):
    """Everything a client needs to operate the instrument (SPEC §7.1).

    Example:
        >>> desc = InstrumentDescriptor(
        ...     identity=IdentityInfo(
        ...         manufacturer="m", model="d", serial_number="s", firmware_version="1"
        ...     ),
        ...     commands=[],
        ...     channels=[],
        ...     interlocks=[],
        ... )
        >>> desc.max_concurrent_commands
        1
    """

    identity: IdentityInfo
    commands: list[CommandSpec]
    channels: list[ChannelSpec]
    interlocks: list[InterlockSpec]
    max_concurrent_commands: int = 1
