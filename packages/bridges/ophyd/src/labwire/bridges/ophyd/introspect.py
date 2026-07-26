"""Derive a draft Labwire descriptor from an ophyd Device.

Pure introspection: reads an ophyd ``Device``'s component structure and
``describe()`` metadata and returns a draft. It performs **no** device I/O
beyond ``describe()``/``describe_configuration()`` (which read cached
metadata), never guesses a missing unit, and never decides on its own that
something is safe. Whatever it cannot resolve is reported by exact component
name for the annotation file (milestone B2) to supply.

Example:
    >>> from ophyd.sim import SynAxis
    >>> from labwire.bridges.ophyd.introspect import introspect
    >>> draft = introspect(SynAxis(name="ax"))
    >>> draft.identity.model
    'SynAxis'
"""

import enum
from typing import Any, Literal, Protocol, cast, runtime_checkable

from labwire.core import IdentityInfo, SafetyClass
from pydantic import BaseModel, ConfigDict

_DTYPE_MAP: dict[str, Literal["float64", "int64", "bool", "string"]] = {
    "number": "float64",
    "integer": "int64",
    "boolean": "bool",
    "string": "string",
}


@runtime_checkable
class OphydDeviceLike(Protocol):
    """The slice of ophyd's Device interface the bridge relies on."""

    name: str

    def describe(self) -> dict[str, Any]:
        """Metadata for read components (Kind normal/hinted)."""
        ...

    def describe_configuration(self) -> dict[str, Any]:
        """Metadata for configuration components (Kind config)."""
        ...


class ComponentRole(enum.StrEnum):
    """What a component becomes in the Labwire descriptor."""

    CHANNEL = "channel"
    """Kind normal/hinted: a telemetry channel, and a set command if settable."""
    CONFIGURATION = "configuration"
    """Kind config: descriptor metadata only, never a settable command."""
    UNSUPPORTED = "unsupported"
    """Present but not representable in Labwire v0.2 (e.g. array-valued)."""


class UnresolvedReason(enum.StrEnum):
    """Why a component cannot yet be exposed."""

    NO_UNIT = "no_unit"
    """No UCUM code from describe(); an annotation must supply one."""
    UNTRANSLATED_EGU = "untranslated_egu"
    """describe() gave an EGU string with no known UCUM translation."""
    UNSUPPORTED_DTYPE = "unsupported_dtype"
    """dtype cannot map to a Labwire channel dtype (arrays, enums)."""


class DraftComponent(BaseModel):
    """One introspected ophyd component.

    Example:
        >>> # draft.component("ax_setpoint").role
    """

    model_config = ConfigDict(frozen=True)

    key: str
    """ophyd's flattened data key, e.g. ``ax_setpoint`` (also the channel name)."""
    attr: str
    """The component attribute on the device, e.g. ``setpoint``."""
    role: ComponentRole
    dtype: Literal["float64", "int64", "bool", "string"] | None
    unit: str | None = None
    unit_source: Literal["describe"] | None = None
    """Where the unit came from; annotations set their own provenance at B2."""
    egu: str | None = None
    """The raw EPICS EGU string, kept for diagnostics even when untranslated."""
    limits: tuple[float, float] | None = None
    settable: bool = False
    source: str | None = None
    """ophyd's ``source`` field (the PV name for EPICS signals)."""


class DraftCommand(BaseModel):
    """One command the bridge will expose.

    Example:
        >>> # {c.name: c.safety_class for c in draft.commands}
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    safety_class: SafetyClass
    component_key: str | None = None
    """The component a ``set_*`` command writes, if any."""


class Unresolved(BaseModel):
    """A gap an annotation file must close, named precisely.

    Example:
        >>> # [u.message for u in draft.unresolved]
    """

    model_config = ConfigDict(frozen=True)

    key: str
    attr: str
    device_class: str
    reason: UnresolvedReason
    message: str


class DraftInstrument(BaseModel):
    """The introspected draft: components, commands, and what is missing.

    Example:
        >>> # draft.is_complete
    """

    model_config = ConfigDict(frozen=True)

    identity: IdentityInfo
    device_class: str
    components: list[DraftComponent]
    commands: list[DraftCommand]
    unresolved: list[Unresolved]

    @property
    def is_complete(self) -> bool:
        """Whether the device can be served with no annotations at all."""
        return not self.unresolved

    def component(self, key: str) -> DraftComponent:
        """Look up a component by its ophyd data key.

        Example:
            >>> # draft.component("ax_setpoint").unit
        """
        for candidate in self.components:
            if candidate.key == key:
                return candidate
        raise KeyError(f"no such component: {key!r}")


def _ophyd_version() -> str:
    try:
        from importlib.metadata import version

        return version("ophyd")
    except Exception:  # pragma: no cover - ophyd is a hard dep of this package
        return "unknown"


def _qualified(obj: object) -> str:
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def _limits_from(described: dict[str, Any]) -> tuple[float, float] | None:
    low = described.get("lower_ctrl_limit")
    high = described.get("upper_ctrl_limit")
    if not isinstance(low, int | float) or not isinstance(high, int | float):
        return None
    # ophyd reports (0, 0) when limits are unset; low == high is absence,
    # never a genuine bound of zero width.
    if low == high:
        return None
    return (float(low), float(high))


def _component_attr(device: object, key: str) -> str:
    """Map ophyd's flattened data key back to the component attribute."""
    name = getattr(device, "name", "")
    if key.startswith(f"{name}_"):
        candidate = key[len(name) + 1 :]
        if hasattr(device, candidate):
            return candidate
    # A positioner's primary readback takes the bare device name; find the
    # component whose own name matches the key.
    for attr in getattr(device, "component_names", ()) or ():
        signal = getattr(device, attr, None)
        if getattr(signal, "name", None) == key:
            return str(attr)
    return key


def _is_settable(device: object, attr: str) -> bool:
    signal = getattr(device, attr, None)
    if signal is None or not hasattr(signal, "set"):
        return False
    # ophyd marks read-only EPICS signals with write_access; sim signals are
    # writable. Absence of the attribute means "no reason to think read-only".
    return bool(getattr(signal, "write_access", True))


def _describe_component(
    device: object,
    key: str,
    described: dict[str, Any],
    role: ComponentRole,
) -> tuple[DraftComponent, Unresolved | None]:
    from labwire.bridges.ophyd._egu import egu_to_ucum

    attr = _component_attr(device, key)
    device_class = _qualified(device)
    raw_dtype = described.get("dtype")
    dtype = _DTYPE_MAP.get(str(raw_dtype)) if raw_dtype is not None else None
    shape = described.get("shape")
    is_array = bool(shape) or dtype is None
    egu = described.get("units")
    egu_text = str(egu) if isinstance(egu, str) else None
    unit = egu_to_ucum(egu_text)

    if is_array:
        component = DraftComponent(
            key=key,
            attr=attr,
            role=ComponentRole.UNSUPPORTED,
            dtype=None,
            egu=egu_text,
            source=described.get("source"),
        )
        return component, Unresolved(
            key=key,
            attr=attr,
            device_class=device_class,
            reason=UnresolvedReason.UNSUPPORTED_DTYPE,
            message=(
                f"{device_class}.{attr} ({key}) has dtype {raw_dtype!r} shape {shape!r}, "
                "which Labwire v0.2 channels cannot carry; exclude it or expose a scalar"
            ),
        )

    component = DraftComponent(
        key=key,
        attr=attr,
        role=role,
        dtype=dtype,
        unit=unit,
        unit_source="describe" if unit else None,
        egu=egu_text,
        limits=_limits_from(described),
        settable=_is_settable(device, attr),
        source=described.get("source"),
    )
    if unit is not None:
        return component, None
    if egu_text and egu_text.strip():
        reason = UnresolvedReason.UNTRANSLATED_EGU
        message = (
            f"{device_class}.{attr} ({key}) reports EGU {egu_text!r}, which has no known "
            "UCUM translation; add a unit in the annotation file"
        )
    else:
        reason = UnresolvedReason.NO_UNIT
        message = (
            f"{device_class}.{attr} ({key}) reports no unit; add a unit in the annotation "
            'file (use "1" only if the quantity really is dimensionless)'
        )
    return component, Unresolved(
        key=key, attr=attr, device_class=device_class, reason=reason, message=message
    )


def _commands_for(device: object, components: list[DraftComponent]) -> list[DraftCommand]:
    """Default command surface and safety classes (SPEC §8.6).

    ``set_*`` is S2 because actuation is what moves hardware; ``trigger`` is
    S1 because acquisition is reversible and consumes nothing; ``stop`` is S0
    so it stays submittable while an interlock is tripped. Annotations may
    raise or lower these, but only explicitly.
    """
    commands: list[DraftCommand] = [
        DraftCommand(
            name="read",
            description="Read every exposed channel once and return the values.",
            safety_class="S1",
        )
    ]
    for component in components:
        if component.role is ComponentRole.CHANNEL and component.settable:
            commands.append(
                DraftCommand(
                    name=f"set_{component.attr}",
                    description=(
                        f"Set {component.attr} and wait for the ophyd status to complete."
                    ),
                    safety_class="S2",
                    component_key=component.key,
                )
            )
    if callable(getattr(device, "trigger", None)):
        commands.append(
            DraftCommand(
                name="trigger",
                description="Trigger an acquisition and wait for it to complete.",
                safety_class="S1",
            )
        )
    if callable(getattr(device, "stop", None)):
        commands.append(
            DraftCommand(
                name="stop",
                description="Stop device motion or acquisition immediately.",
                safety_class="S0",
            )
        )
    return commands


def introspect(device: Any) -> DraftInstrument:
    """Derive a draft Labwire descriptor from an ophyd Device.

    Kind ``hinted``/``normal`` components become channels, ``config``
    components become descriptor metadata, and ``omitted`` components are
    skipped. Units are adopted from ``describe()`` when present and
    translated from EGU; anything unresolved is listed in
    :attr:`DraftInstrument.unresolved` rather than defaulted.

    ``device`` is any ophyd ``Device``. It is typed ``Any`` deliberately:
    ophyd ships no type information, so static checking cannot verify the
    shape here. :class:`OphydDeviceLike` documents the interface actually
    used and is ``runtime_checkable`` for callers who want to assert it.

    Example:
        >>> from ophyd.sim import SynAxis
        >>> introspect(SynAxis(name="ax")).is_complete  # sim carries no units
        False
    """
    device_class = _qualified(device)
    components: list[DraftComponent] = []
    unresolved: list[Unresolved] = []

    for role, described_map in (
        (ComponentRole.CHANNEL, device.describe()),
        (ComponentRole.CONFIGURATION, device.describe_configuration()),
    ):
        for key, raw in cast("dict[str, Any]", described_map).items():
            described = cast("dict[str, Any]", raw)
            component, gap = _describe_component(device, str(key), described, role)
            components.append(component)
            if gap is not None:
                unresolved.append(gap)

    identity = IdentityInfo(
        manufacturer="ophyd bridge (Labwire)",
        model=type(device).__name__,
        serial_number=device.name,
        firmware_version=f"ophyd {_ophyd_version()}",
    )
    return DraftInstrument(
        identity=identity,
        device_class=device_class,
        components=components,
        commands=_commands_for(device, components),
        unresolved=unresolved,
    )
