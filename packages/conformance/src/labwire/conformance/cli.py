"""The labwire-conformance command."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from labwire.conformance._checks import RunOptions
from labwire.conformance._report import LEVELS
from labwire.conformance.runner import run_suite

app = typer.Typer(add_completion=False, help="Prove a Labwire server conformant, or see why not.")


@app.command()
def run(
    url: Annotated[str, typer.Argument(help="WebSocket endpoint, e.g. ws://127.0.0.1:9500")],
    exercise: Annotated[
        str | None,
        typer.Option(help="Name of one SAFE command to actually execute (opt-in)."),
    ] = None,
    params: Annotated[str, typer.Option(help="JSON params for the exercised command.")] = "{}",
    confirmation: Annotated[
        str | None, typer.Option(help="Confirmation string for an S2 exercised command.")
    ] = None,
    authorization: Annotated[
        str | None, typer.Option(help="Operator grant id for an S3 exercised command.")
    ] = None,
    bundle_dir: Annotated[
        Path | None, typer.Option(help="Directory where the server writes signed run bundles.")
    ] = None,
    claim: Annotated[
        str, typer.Option(help="Level the server claims: core, streaming, or signed.")
    ] = "core",
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Also write the report as JSON here.")
    ] = None,
) -> None:
    """Run every applicable check and exit 0 only if the claimed level holds."""
    if claim not in LEVELS:
        typer.echo(f"unknown level {claim!r}; pick one of {', '.join(LEVELS)}", err=True)
        raise typer.Exit(2)
    options = RunOptions(
        exercise=exercise,
        exercise_params=json.loads(params),
        confirmation=confirmation,
        authorization=authorization,
        bundle_dir=bundle_dir,
    )
    report = asyncio.run(run_suite(url, options))
    typer.echo(report.render())
    if json_out is not None:
        json_out.write_text(report.to_json() + "\n")
        typer.echo(f"json report: {json_out}")
    achieved, _ = report.verdict()
    achieved_rank = LEVELS.index(achieved) if achieved in LEVELS else -1
    if achieved_rank >= LEVELS.index(claim):
        raise typer.Exit(0)
    typer.echo(f"claimed level {claim!r} NOT proven (achieved: {achieved})", err=True)
    raise typer.Exit(1)


def main() -> None:
    """Entry point."""
    sys.exit(app())


if __name__ == "__main__":
    main()
