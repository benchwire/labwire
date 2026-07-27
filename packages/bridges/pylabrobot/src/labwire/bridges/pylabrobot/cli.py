"""``labwire-pylabrobot``: inspect what the bridge would expose from a deck.

Unlike the ophyd bridge's CLI, there is no ``annotate`` subcommand. That one
exists because ophyd devices carry no units, so a starter file with the gaps
filled in is most of the work. PyLabRobot is consistent about units by
convention, so its annotation file is optional and there is nothing to
scaffold. What is worth checking is that a file matches the deck it claims to
describe, which is what ``check`` does.

A liquid handler needs a configured deck, so the target is a factory rather
than a module attribute: ``module:function`` returning a ``LiquidHandler``,
awaited if it is a coroutine.

The target module has to be importable, so run this from a directory on the
Python path. From a checkout of this repository, that means the repository
root, with the example rig reachable as
``examples.liquid_handling.rig:build_liquid_handler``.

Example:
    labwire-pylabrobot check mypackage.rig:build_handler -a labwire-pylabrobot.yaml
"""

import asyncio
import importlib
import inspect
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from labwire.bridges.pylabrobot.annotations import AnnotationError, AnnotationFile, load_annotations
from labwire.bridges.pylabrobot.deck import deck_state
from labwire.bridges.pylabrobot.introspect import introspect

app = typer.Typer(
    add_completion=False,
    help="Inspect what Labwire would expose from a PyLabRobot liquid handler.",
)


@app.callback()
def _root() -> None:  # pyright: ignore[reportUnusedFunction]
    """Keep ``check`` a subcommand.

    Typer promotes a lone command to the root, which would make the interface
    ``labwire-pylabrobot <target>`` and leave no room to add another later.
    """


def _load(target: str) -> Any:
    """Import ``module:factory`` and call it, awaiting a coroutine if needed."""
    if ":" not in target:
        raise typer.BadParameter(
            f"expected 'module:factory', got {target!r} "
            "(the factory must return a configured LiquidHandler)"
        )
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise typer.BadParameter(f"cannot import {module_name!r}: {exc}") from exc
    factory = getattr(module, attribute, None)
    if factory is None:
        raise typer.BadParameter(f"{module_name!r} has no attribute {attribute!r}")
    result = factory() if callable(factory) else factory
    if inspect.isawaitable(result):
        return asyncio.run(cast("Coroutine[Any, Any, Any]", result))
    return result


@app.command()
def check(
    target: Annotated[str, typer.Argument(help="module:factory returning a LiquidHandler")],
    annotations: Annotated[
        Path | None, typer.Option("--annotations", "-a", help="Annotation file to apply")
    ] = None,
) -> None:
    """Print the instrument Labwire would serve, or explain what is wrong."""
    handler = _load(target)
    try:
        parsed = load_annotations(annotations) if annotations else AnnotationFile()
    except AnnotationError as exc:
        for problem in exc.problems:
            typer.echo(f"  {problem}", err=True)
        raise typer.Exit(1) from exc

    draft = introspect(handler)
    state = deck_state(handler, parsed)

    typer.echo(f"OK: {draft.identity.model} ({draft.identity.serial_number})")
    typer.echo(f"  {draft.channel_count} channel(s), {len(state.labware)} piece(s) of labware")
    for spec in draft.commands:
        override = parsed.commands.get(spec.name)
        if override is not None and override.exclude:
            typer.echo(f"    --  {spec.name} (excluded by annotation)")
            continue
        safety = (override.safety_class if override else None) or spec.safety_class
        typer.echo(f"    {safety}  {spec.name}")
    for item in state.labware:
        grid = f" {item.grid.rows}x{item.grid.columns}" if item.grid else ""
        notes = []
        if item.hazard:
            notes.append(f"hazard: {item.hazard}")
        if item.locked:
            notes.append("LOCKED")
        if item.tips_available is not None:
            notes.append(f"{item.tips_available} tips")
        suffix = f"  ({', '.join(notes)})" if notes else ""
        typer.echo(f"    {item.uri}: {item.kind}{grid}{suffix}")

    for gap in draft.unresolved:
        typer.echo(f"  note: {gap.message}", err=True)


def main() -> None:
    """Entry point for the ``labwire-pylabrobot`` console script."""
    try:
        app()
    except AnnotationError as exc:  # pragma: no cover - typer handles its own exits
        for problem in exc.problems:
            typer.echo(f"  {problem}", err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
