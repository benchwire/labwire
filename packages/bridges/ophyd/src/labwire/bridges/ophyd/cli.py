"""The ``labwire-ophyd`` command line: generate and check annotation files.

Example:
    >>> # $ labwire-ophyd annotate ophyd.sim:motor -o labwire-ophyd.yaml
    >>> # $ labwire-ophyd check ophyd.sim:motor --annotations labwire-ophyd.yaml
"""

import importlib
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from labwire.bridges.ophyd.annotations import (
    AnnotationError,
    AnnotationFile,
    ResolvedInstrument,
    load_annotations,
    resolve,
)
from labwire.bridges.ophyd.introspect import DraftInstrument, introspect

TODO_UNIT = "TODO-unit"
"""Placeholder for a unit the bridge could not resolve. Never a real default."""

app = typer.Typer(
    help="Bridge ophyd devices into Labwire: generate and check annotation files.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:  # pyright: ignore[reportUnusedFunction] - registered by typer
    """Labwire ophyd bridge tools."""


def _load_device(target: str) -> Any:
    """Import ``module:attribute`` and return the ophyd device it names."""
    if ":" not in target:
        typer.echo(
            f"error: {target!r} is not a device target; use module:attribute "
            "(for example ophyd.sim:motor)",
            err=True,
        )
        raise typer.Exit(2)
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        typer.echo(f"error: cannot import module {module_name!r}: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        device = getattr(module, attribute)
    except AttributeError as exc:
        typer.echo(f"error: {module_name!r} has no attribute {attribute!r}", err=True)
        raise typer.Exit(2) from exc
    return device() if callable(device) and not hasattr(device, "describe") else device


def _starter_document(draft: DraftInstrument) -> dict[str, Any]:
    """Build a starter annotation file, marking every gap rather than guessing."""
    components: dict[str, Any] = {}
    for component in draft.components:
        entry: dict[str, Any] = {}
        entry["unit"] = component.unit if component.unit else TODO_UNIT
        if component.dtype is not None:
            # Written out because ophyd infers dtype from the value a signal
            # happens to hold: an axis resting at 0 reports "integer", which
            # would reject a fractional move until this is corrected.
            entry["dtype"] = component.dtype
        if component.egu and not component.unit:
            entry["description"] = f"TODO: device reports EGU {component.egu!r}"
        if component.limits is not None:
            low, high = component.limits
            entry["limits"] = {"low": low, "high": high}
        components[component.attr] = entry
    device_class = draft.mro_root()
    return {
        "version": 1,
        "devices": {
            device_class: {
                "description": f"TODO: what {draft.identity.model} is and what it is for.",
                "intent_tags": [],
                "components": components,
            }
        },
    }


def _describe_resolution(resolved: ResolvedInstrument) -> str:
    channels = [c for c in resolved.components if c.role.value == "channel"]
    lines = [
        f"OK: {resolved.identity.model} ({resolved.identity.serial_number})",
        f"  {len(channels)} channel(s), {len(resolved.components)} component(s), "
        f"{len(resolved.commands)} command(s)",
    ]
    for command in resolved.commands:
        lines.append(f"    {command.safety_class}  {command.name}")
    for component in resolved.components:
        lines.append(f"    {component.key}: {component.unit}")
    if resolved.omitted:
        lines.append(f"  omitted {len(resolved.omitted)} component(s) under --allow-partial:")
        lines.extend(f"    {gap.key}: {gap.reason.value}" for gap in resolved.omitted)
    return "\n".join(lines)


@app.command()
def annotate(
    target: Annotated[str, typer.Argument(help="Device as module:attribute, e.g. ophyd.sim:motor")],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write here")] = None,
) -> None:
    """Emit a starter annotation file, with every unresolved gap marked TODO.

    The generated file is valid YAML that loads as-is; filling in the TODO
    placeholders is what makes the device servable.
    """
    draft = introspect(_load_device(target))
    document = yaml.safe_dump(_starter_document(draft), sort_keys=False, allow_unicode=True)
    banner = (
        "# Labwire annotation file for an ophyd device.\n"
        "# Replace every TODO: a unit is never guessed for you (SPEC §7.2).\n"
        '# Units are UCUM codes; use "1" only for genuinely dimensionless values.\n'
    )
    if output is None:
        typer.echo(banner + document)
        return
    output.write_text(banner + document)
    gaps = len(draft.unresolved)
    typer.echo(f"wrote {output} ({gaps} gap(s) marked {TODO_UNIT})")


@app.command()
def check(
    target: Annotated[str, typer.Argument(help="Device as module:attribute")],
    annotations: Annotated[
        Path | None, typer.Option("--annotations", "-a", help="Annotation file")
    ] = None,
    allow_partial: Annotated[
        bool, typer.Option("--allow-partial", help="Omit unresolved components instead of failing")
    ] = False,
) -> None:
    """Resolve a device against annotations and report what Labwire would expose.

    Exits 1 with every problem listed when the device cannot be resolved.
    """
    draft = introspect(_load_device(target))
    try:
        loaded = load_annotations(annotations) if annotations else AnnotationFile()
        resolved = resolve(draft, loaded, allow_partial=allow_partial)
    except AnnotationError as exc:
        for problem in exc.problems:
            typer.echo(f"error: {problem}", err=True)
        typer.echo(f"FAILED: {len(exc.problems)} problem(s)", err=True)
        raise typer.Exit(1) from exc
    typer.echo(_describe_resolution(resolved))


def main() -> None:
    """Console-script entry point."""
    app()
