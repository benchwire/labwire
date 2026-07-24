"""The ``labwire`` command-line tool.

Example:
    >>> # $ labwire verify runs/<run_id>
"""

import json
from pathlib import Path

import typer
from labwire.core.signing import Manifest, verify_bundle

app = typer.Typer(
    help="Labwire: open protocol for AI-controlled lab instruments.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:  # pyright: ignore[reportUnusedFunction] - registered by typer
    """Labwire command-line tools."""
    # A callback keeps `verify` a subcommand even while it is the only one.


@app.command()
def verify(bundle: Path) -> None:
    """Verify a signed run bundle (a directory with manifest.json, or the file itself).

    Checks the ed25519 signature over the JCS-canonicalized manifest, the
    signer key_id, and — when records.jsonl is present — recomputes the
    record-stream digest (SPEC §12.2). Exits 0 when authentic, 1 otherwise.
    """
    outcome = verify_bundle(bundle)
    for warning in outcome.warnings:
        typer.echo(f"warning: {warning}")
    if not outcome.ok:
        for error in outcome.errors:
            typer.echo(f"error: {error}", err=True)
        typer.echo("FAILED: bundle is not authentic", err=True)
        raise typer.Exit(1)
    manifest_path = bundle if bundle.is_file() else bundle / "manifest.json"
    manifest = Manifest.model_validate(json.loads(manifest_path.read_text()))
    identity = manifest.instrument
    typer.echo(f"OK: run {manifest.run_id}")
    typer.echo(
        f"  instrument: {identity.manufacturer} {identity.model} (SN {identity.serial_number})"
    )
    typer.echo(f"  command:    {manifest.command.name} {json.dumps(manifest.command.params)}")
    typer.echo(f"  status:     {manifest.status}")
    typer.echo(f"  completed:  {manifest.timestamps.completed}")
    if manifest.signer is not None:
        typer.echo(f"  signed by:  {manifest.signer.key_id}")


def main() -> None:
    """Console-script entry point."""
    app()
