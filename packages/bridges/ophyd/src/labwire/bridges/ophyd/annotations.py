"""The annotation file: the semantics ophyd never carried.

Introspection (:mod:`labwire.bridges.ophyd.introspect`) gets the mechanical
80% of a descriptor from an ophyd ``Device``. This module supplies the rest —
units where EPICS gave none, safety classes ophyd has no concept of, physical
limits, human descriptions, intent tags — from a YAML sidecar, and refuses to
resolve an instrument whose gaps are still open.

Annotations are keyed by ophyd class (inherited along the MRO, so a subclass
gets its base's entries) and refined per device instance. Merging is
per-field: a subclass or instance overrides only what it names.

Example:
    >>> from labwire.bridges.ophyd.annotations import AnnotationFile, resolve
    >>> # resolved = resolve(introspect(device), load_annotations(path))
"""

from pathlib import Path
from typing import Any, Literal, Self

import yaml
from labwire.bridges.ophyd.introspect import (
    ComponentRole,
    DraftInstrument,
    Unresolved,
    UnresolvedReason,
)
from labwire.core import SafetyClass
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

SUPPORTED_VERSION = 1

Dtype = Literal["float64", "int64", "bool", "string"]


class AnnotationError(Exception):
    """An annotation file is invalid, or an instrument cannot be resolved.

    Carries every problem found, so one run tells the author everything they
    need to fix instead of one item at a time.

    Example:
        >>> # except AnnotationError as exc: print(exc)
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("\n".join(problems))


class _Strict(BaseModel):
    """Unknown keys are errors: a typo must never silently annotate nothing."""

    model_config = ConfigDict(extra="forbid")


class Limits(_Strict):
    """A physical bound, tighter than or equal to the device's own limits.

    Example:
        >>> Limits(low=-5.0, high=5.0).as_tuple()
        (-5.0, 5.0)
    """

    low: float
    high: float

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.low >= self.high:
            raise ValueError(f"limits low ({self.low}) must be below high ({self.high})")
        return self

    def as_tuple(self) -> tuple[float, float]:
        """Return the bound as a plain ``(low, high)`` tuple."""
        return (self.low, self.high)


class ComponentAnnotation(_Strict):
    """Semantics for one ophyd component.

    Example:
        >>> ComponentAnnotation(unit="mm", safety_class="S3").unit
        'mm'
    """

    unit: str | None = None
    """UCUM code. Supply ``"1"`` only when the quantity really is dimensionless."""
    qudt_quantity_kind: str | None = None
    description: str | None = None
    dtype: Dtype | None = None
    """Override ophyd's value-inferred dtype."""
    limits: Limits | None = None
    safety_class: SafetyClass | None = None
    """Overrides the class of this component's generated ``set_*`` command."""
    exclude: bool = False
    """Drop this component deliberately (not counted as an unresolved gap)."""


class CommandAnnotation(_Strict):
    """Overrides for one generated command.

    Example:
        >>> CommandAnnotation(safety_class="S3").safety_class
        'S3'
    """

    safety_class: SafetyClass | None = None
    description: str | None = None
    estimated_duration_s: float | None = None


class DeviceAnnotation(_Strict):
    """Semantics for one ophyd device class or instance.

    Example:
        >>> DeviceAnnotation(description="A stage.").intent_tags
        []
    """

    description: str | None = None
    intent_tags: list[str] = []
    components: dict[str, ComponentAnnotation] = {}
    commands: dict[str, CommandAnnotation] = {}


class AnnotationFile(_Strict):
    """A parsed annotation file.

    Example:
        >>> AnnotationFile().version
        1
    """

    version: int = SUPPORTED_VERSION
    devices: dict[str, DeviceAnnotation] = {}
    """Keyed by dotted ophyd class path, inherited along the MRO."""
    instances: dict[str, DeviceAnnotation] = {}
    """Keyed by ``Device.name``; overrides the class entry."""

    @model_validator(mode="after")
    def _supported_version(self) -> Self:
        if self.version != SUPPORTED_VERSION:
            raise ValueError(
                f"annotation file version {self.version} is not supported "
                f"(this build understands version {SUPPORTED_VERSION})"
            )
        return self


class ResolvedComponent(BaseModel):
    """A component with everything Labwire needs to expose it.

    Example:
        >>> # resolved.component("stage_x").unit
    """

    model_config = ConfigDict(frozen=True)

    key: str
    attr: str
    role: ComponentRole
    dtype: Dtype
    unit: str
    qudt_quantity_kind: str | None = None
    description: str
    limits: tuple[float, float] | None = None
    settable: bool = False
    source: str | None = None


class ResolvedCommand(BaseModel):
    """A command with its final safety class and description.

    Example:
        >>> # {c.name: c.safety_class for c in resolved.commands}
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    safety_class: SafetyClass
    component_key: str | None = None
    estimated_duration_s: float | None = None


class ResolvedInstrument(BaseModel):
    """An ophyd device fully described in Labwire terms.

    Example:
        >>> # resolve(draft, annotations).is_complete
    """

    model_config = ConfigDict(frozen=True)

    identity: Any
    device_class: str
    description: str
    intent_tags: list[str] = []
    components: list[ResolvedComponent]
    commands: list[ResolvedCommand]
    omitted: list[Unresolved] = []
    """Gaps skipped under ``allow_partial``; empty when fully resolved."""

    def component(self, key: str) -> ResolvedComponent:
        """Look up an exposed component by its ophyd data key.

        Example:
            >>> # resolved.component("ax_setpoint").limits
        """
        for candidate in self.components:
            if candidate.key == key:
                return candidate
        raise KeyError(f"no such exposed component: {key!r}")


def load_annotations(path: Path) -> AnnotationFile:
    """Load and validate an annotation file.

    Example:
        >>> # annotations = load_annotations(Path("labwire-ophyd.yaml"))
    """
    if not path.exists():
        raise AnnotationError([f"annotation file not found: {path}"])
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise AnnotationError([f"{path}: invalid YAML: {exc}"]) from exc
    if not isinstance(raw, dict):
        raise AnnotationError([f"{path}: expected a mapping at the top level"])
    try:
        return AnnotationFile.model_validate(raw)
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            where = ".".join(str(part) for part in error["loc"]) or "<root>"
            got = error.get("input")
            detail = f"{error['msg']} (got {got!r})" if got is not None else error["msg"]
            problems.append(f"{path}: {where}: {detail}")
        raise AnnotationError(problems) from exc


def _mro_class_paths(device_class: str, mro: list[str]) -> list[str]:
    """Class paths from most general to most specific, so subclasses win."""
    ordered = [path for path in reversed(mro) if path not in {"builtins.object"}]
    if device_class not in ordered:
        ordered.append(device_class)
    return ordered


def _merge_component(
    layers: list[ComponentAnnotation],
) -> ComponentAnnotation:
    """Later layers override earlier ones field by field."""
    merged: dict[str, Any] = {}
    for layer in layers:
        merged.update(layer.model_dump(exclude_unset=True, exclude_none=True))
    return ComponentAnnotation.model_validate(merged)


def _merge_command(layers: list[CommandAnnotation]) -> CommandAnnotation:
    merged: dict[str, Any] = {}
    for layer in layers:
        merged.update(layer.model_dump(exclude_unset=True, exclude_none=True))
    return CommandAnnotation.model_validate(merged)


def _intersect(
    reported: tuple[float, float] | None, annotated: tuple[float, float] | None
) -> tuple[float, float] | None:
    """The tightest bound on each side wins (see DESIGN.md)."""
    if reported is None:
        return annotated
    if annotated is None:
        return reported
    return (max(reported[0], annotated[0]), min(reported[1], annotated[1]))


def resolve(
    draft: DraftInstrument,
    annotations: AnnotationFile,
    *,
    allow_partial: bool = False,
    mro: list[str] | None = None,
) -> ResolvedInstrument:
    """Merge annotations over an introspected draft, or refuse.

    Layers, later winning per field: the draft, then each class entry from
    the most general base to the device's own class, then the instance entry.
    Every remaining gap is reported at once, naming the exact component; with
    ``allow_partial`` the offending components are omitted and listed in
    :attr:`ResolvedInstrument.omitted` instead.

    Example:
        >>> # resolve(introspect(device), load_annotations(path))
    """
    instance_name = draft.identity.serial_number
    class_paths = _mro_class_paths(
        draft.device_class, mro or draft.class_mro or [draft.device_class]
    )
    device_layers = [
        annotations.devices[path] for path in class_paths if path in annotations.devices
    ]
    instance_layer = annotations.instances.get(instance_name)
    all_layers = [*device_layers, *([instance_layer] if instance_layer else [])]

    problems: list[str] = []

    # An annotation that names something the device does not have is a typo,
    # not a no-op: report it rather than silently ignoring it.
    known_components = {c.attr for c in draft.components}
    known_commands = {c.name for c in draft.commands}
    for layer in all_layers:
        for attr in layer.components:
            if attr not in known_components:
                problems.append(
                    f"{draft.device_class}: annotation names component {attr!r}, which the "
                    f"device does not have (known: {sorted(known_components)})"
                )
        for name in layer.commands:
            if name not in known_commands:
                problems.append(
                    f"{draft.device_class}: annotation names command {name!r}, which the "
                    f"bridge does not generate (known: {sorted(known_commands)})"
                )

    components: list[ResolvedComponent] = []
    omitted: list[Unresolved] = []
    component_overrides: dict[str, ComponentAnnotation] = {}

    for component in draft.components:
        layers = [
            layer.components[component.attr]
            for layer in all_layers
            if component.attr in layer.components
        ]
        merged = _merge_component(layers)
        component_overrides[component.attr] = merged
        if merged.exclude:
            continue

        unit = merged.unit or component.unit
        dtype = merged.dtype or component.dtype
        gap: Unresolved | None = None
        if component.role is ComponentRole.UNSUPPORTED:
            gap = next(
                (u for u in draft.unresolved if u.key == component.key),
                Unresolved(
                    key=component.key,
                    attr=component.attr,
                    device_class=draft.device_class,
                    reason=UnresolvedReason.UNSUPPORTED_DTYPE,
                    message=f"{component.key} cannot be exposed",
                ),
            )
        elif unit is None or dtype is None:
            missing = "unit" if unit is None else "dtype"
            gap = Unresolved(
                key=component.key,
                attr=component.attr,
                device_class=draft.device_class,
                reason=UnresolvedReason.NO_UNIT,
                message=(
                    f"{draft.device_class}.{component.attr} ({component.key}) has no {missing}: "
                    f"add `devices: {{{draft.device_class}: {{components: {{{component.attr}: "
                    f"{{{missing}: ...}}}}}}}}` to the annotation file"
                ),
            )

        if gap is not None:
            if allow_partial:
                omitted.append(gap)
                continue
            problems.append(gap.message)
            continue

        assert unit is not None
        assert dtype is not None
        components.append(
            ResolvedComponent(
                key=component.key,
                attr=component.attr,
                role=component.role,
                dtype=dtype,
                unit=unit,
                qudt_quantity_kind=merged.qudt_quantity_kind,
                description=merged.description or f"{component.attr} ({component.key})",
                limits=_intersect(
                    component.limits, merged.limits.as_tuple() if merged.limits else None
                ),
                settable=component.settable,
                source=component.source,
            )
        )

    if problems:
        raise AnnotationError(problems)

    exposed_keys = {c.key for c in components}
    commands: list[ResolvedCommand] = []
    for command in draft.commands:
        if command.component_key is not None and command.component_key not in exposed_keys:
            continue  # its component was excluded or omitted
        layers = [
            layer.commands[command.name] for layer in all_layers if command.name in layer.commands
        ]
        merged_command = _merge_command(layers)
        # A component-level safety_class applies to that component's set command.
        component_class: SafetyClass | None = None
        if command.component_key is not None:
            attr = next(c.attr for c in components if c.key == command.component_key)
            component_class = component_overrides.get(attr, ComponentAnnotation()).safety_class
        commands.append(
            ResolvedCommand(
                name=command.name,
                description=merged_command.description or command.description,
                safety_class=merged_command.safety_class or component_class or command.safety_class,
                component_key=command.component_key,
                estimated_duration_s=merged_command.estimated_duration_s,
            )
        )

    description = next(
        (layer.description for layer in reversed(all_layers) if layer.description),
        f"{draft.identity.model} exposed through the Labwire ophyd bridge.",
    )
    intent_tags: list[str] = []
    for layer in all_layers:
        for tag in layer.intent_tags:
            if tag not in intent_tags:
                intent_tags.append(tag)

    return ResolvedInstrument(
        identity=draft.identity,
        device_class=draft.device_class,
        description=description,
        intent_tags=intent_tags,
        components=components,
        commands=commands,
        omitted=omitted,
    )
