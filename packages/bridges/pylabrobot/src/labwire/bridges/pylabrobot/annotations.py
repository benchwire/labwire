"""The annotation file: what a deck means, which PyLabRobot cannot know.

The ophyd bridge's annotation file exists mostly to supply units, because
ophyd carries none. PyLabRobot is consistent by convention (microlitres,
microlitres per second, millimetres), so the bridge supplies units from a
built-in table and this file is **optional**. What it is actually for is the
thing neither library models: what is in the labware.

That difference forces a new kind of key. The ophyd format annotates
components and commands, which are static structure. Here the annotation you
want is *the plate named ``acid_stock`` holds something corrosive*, keyed by a
name someone chose when they loaded the deck this morning. So the file has
three sections: ``instrument``, ``commands``, and ``resources`` (with
``labware`` for defaults shared by every item of a labware model).

    version: 1
    instrument:
      description: A STARlet running dilutions.
    commands:
      transfer: {estimated_duration_s: 4.0}
    labware:
      Cor_96_wellplate_360ul_Fb: {description: A 96-well Costar plate.}
    resources:
      labwire:deck/acid_stock:
        description: 1 M hydrochloric acid.
        hazard: corrosive
        locked: true

There is no per-resource ``safety_class`` any more. It was documented in
three places as reported-but-not-enforced, and keeping a field that still
cannot raise a call's class would be keeping a lie; argument-dependent
classes are finding F3, out of scope for v0.3. What is enforced: ``locked``
refuses every operation touching the resource, and command-level
``safety_class`` overrides now genuinely bite, because raising a command to
S3 makes it require an operator grant (SPEC 8.6). ``hazard`` appears in the
deck resource content, so an agent reading ``labwire:deck`` sees which
labware is dangerous even though the protocol cannot yet grade the call.

Example:
    >>> AnnotationFile().version
    1
"""

from pathlib import Path
from typing import Any, Self

import yaml
from labwire.core import SafetyClass
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

SUPPORTED_VERSION = 1


class AnnotationError(Exception):
    """An annotation file is invalid, or does not match the deck.

    Carries every problem found, so one run tells the author everything to
    fix rather than one item at a time.

    Example:
        >>> AnnotationError(["a", "b"]).problems
        ['a', 'b']
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("\n".join(problems))


class _Strict(BaseModel):
    """Unknown keys are errors: a typo must never silently annotate nothing."""

    model_config = ConfigDict(extra="forbid")


class ResourceAnnotation(_Strict):
    """What a piece of labware, or one model of labware, contains.

    Example:
        >>> ResourceAnnotation(hazard="corrosive", locked=True).locked
        True
    """

    description: str | None = None
    hazard: str | None = None
    """Free text, surfaced to agents in the deck resource content."""
    locked: bool = False
    """Refuse every operation that touches this resource.

    The only escalation Labwire v0.2 can actually enforce, so it is a hard
    refusal rather than a gradation.
    """


class CommandAnnotation(_Strict):
    """Overrides for one exposed command.

    Example:
        >>> CommandAnnotation(safety_class="S3").safety_class
        'S3'
    """

    safety_class: SafetyClass | None = None
    description: str | None = None
    estimated_duration_s: float | None = None
    exclude: bool = False
    """Drop a command this deployment should not offer at all."""


class InstrumentAnnotation(_Strict):
    """Descriptive metadata for the liquid handler itself.

    Example:
        >>> InstrumentAnnotation().intent_tags
        []
    """

    description: str | None = None
    intent_tags: list[str] = []


class AnnotationFile(_Strict):
    """A parsed annotation file.

    Example:
        >>> AnnotationFile().resources
        {}
    """

    version: int = SUPPORTED_VERSION
    instrument: InstrumentAnnotation = InstrumentAnnotation()
    commands: dict[str, CommandAnnotation] = {}
    labware: dict[str, ResourceAnnotation] = {}
    """Keyed by PyLabRobot labware model or class name; defaults for every instance."""
    resources: dict[str, ResourceAnnotation] = {}
    """Keyed by the labware's deck URI (``labwire:deck/<name>``); overrides
    the labware entry per field."""

    @model_validator(mode="after")
    def _supported_version(self) -> Self:
        if self.version != SUPPORTED_VERSION:
            raise ValueError(
                f"annotation file version {self.version} is not supported "
                f"(this build understands version {SUPPORTED_VERSION})"
            )
        return self


def load_annotations(path: Path) -> AnnotationFile:
    """Load and validate an annotation file.

    Example:
        >>> # annotations = load_annotations(Path("labwire-pylabrobot.yaml"))
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
        problems: list[str] = []
        for error in exc.errors():
            where = ".".join(str(part) for part in error["loc"]) or "<root>"
            got = error.get("input")
            detail = f"{error['msg']} (got {got!r})" if got is not None else error["msg"]
            problems.append(f"{path}: {where}: {detail}")
        raise AnnotationError(problems) from exc


def _merge(layers: list[ResourceAnnotation]) -> ResourceAnnotation:
    """Later layers override earlier ones field by field."""
    merged: dict[str, Any] = {}
    for layer in layers:
        merged.update(layer.model_dump(exclude_unset=True, exclude_none=True))
    return ResourceAnnotation.model_validate(merged)


def annotation_for(
    annotations: AnnotationFile,
    *,
    uri: str,
    model: str | None = None,
    type_name: str | None = None,
) -> ResourceAnnotation:
    """The annotation in force for one resource, merged per field.

    Layers, later winning: the labware entry keyed by PyLabRobot class, then
    the one keyed by labware model, then the labware's deck URI.

    Example:
        >>> annotation_for(AnnotationFile(), uri="labwire:deck/plate").locked
        False
    """
    layers = [
        annotations.labware[key]
        for key in (type_name, model)
        if key is not None and key in annotations.labware
    ]
    if uri in annotations.resources:
        layers.append(annotations.resources[uri])
    return _merge(layers)


def check(
    annotations: AnnotationFile,
    *,
    known_resources: set[str],
    known_labware: set[str],
    known_commands: set[str],
) -> None:
    """Refuse an annotation file that names something that is not there.

    An annotation naming a resource this deck does not hold is a typo, and a
    silently ignored hazard annotation is the worst possible failure mode for
    this particular file. Every problem is reported at once.

    Example:
        >>> check(AnnotationFile(), known_resources=set(), known_labware=set(),
        ...       known_commands=set())
    """
    problems: list[str] = []
    for name in sorted(annotations.resources):
        if name not in known_resources:
            problems.append(
                f"annotation names resource {name!r}, which is not on the deck "
                f"(known: {sorted(known_resources)})"
            )
    for key in sorted(annotations.labware):
        if key not in known_labware:
            problems.append(
                f"annotation names labware {key!r}, which is not on the deck "
                f"(known: {sorted(known_labware)})"
            )
    for name in sorted(annotations.commands):
        if name not in known_commands:
            problems.append(
                f"annotation names command {name!r}, which the bridge does not expose "
                f"(known: {sorted(known_commands)})"
            )
    if problems:
        raise AnnotationError(problems)
