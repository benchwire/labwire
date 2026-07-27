"""The ``labwire`` command-line tool.

Example:
    >>> # $ labwire verify runs/<run_id>
"""

import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from labwire.core.signing import Manifest, verify_bundle

if TYPE_CHECKING:
    from labwire.core.grants import GrantStore

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
    signer key_id, and, when records.jsonl is present, recomputes the
    record-stream digest (SPEC §13.2). Exits 0 when authentic, 1 otherwise.
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
    if manifest.command.safety_class is not None:
        typer.echo(f"  class:      {manifest.command.safety_class}")
    if manifest.authorization is not None:
        auth = manifest.authorization
        if auth.mode == "grant":
            use = f", use {auth.use_index}" if auth.use_index is not None else ""
            who = f', issued_by "{auth.issued_by}" [unauthenticated note]' if auth.issued_by else ""
            typer.echo(f"  authorized: grant{use}, request {auth.request_id or '?'}{who}")
        else:
            typer.echo(f"  authorized: {auth.mode}")
        # The honesty caveat as a machine-checkable wire fact (SPEC 13.1):
        # a v0.3 bundle claiming identity was verified is not a v0.3 bundle.
        if manifest.manifest_version == "0.3" and auth.identity_verified:
            typer.echo(
                "error: identity_verified is true in a 0.3 manifest; v0.3 proves "
                "deployment policy and parameter binding, NOT operator identity",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(
            "              deployment policy and parameter binding proven; "
            "operator identity NOT proven"
        )
    typer.echo(f"  completed:  {manifest.timestamps.completed}")
    if manifest.signer is not None:
        typer.echo(f"  signed by:  {manifest.signer.key_id}")


grant_app = typer.Typer(
    help="Operator grant store: approve or revoke S3 authorization requests.",
    no_args_is_help=True,
)
app.add_typer(grant_app, name="grant")


def _store(directory: Path | None) -> "GrantStore":
    import os

    from labwire.core.grants import GrantStore

    where = directory or (Path(root) if (root := os.environ.get("LABWIRE_GRANT_STORE")) else None)
    if where is None:
        typer.echo("error: no grant store; pass --store or set LABWIRE_GRANT_STORE", err=True)
        raise typer.Exit(2)
    serial = "unknown"
    grants_file = where / "grants.json"
    if grants_file.exists():
        with contextlib.suppress(ValueError):
            serial = (
                json.loads(grants_file.read_text()).get("instrument", {}).get("serial_number")
                or serial
            )
    pending_file = where / "pending.jsonl"
    if serial == "unknown" and pending_file.exists():
        for line in pending_file.read_text().splitlines():
            try:
                serial = json.loads(line).get("serial_number") or serial
                break
            except ValueError:
                continue
    return GrantStore(where, serial_number=serial)


@grant_app.command("list")
def grant_list(
    store: Annotated[Path | None, typer.Option("--store", help="Grant store directory")] = None,
) -> None:
    """Show pending authorization requests, with their exact parameters.

    This listing is the whole point of the pending-request flow: the
    operator reads what was asked from the server's own record, never from
    a digest relayed through the agent that wants the approval.
    """
    from datetime import UTC, datetime

    grant_store = _store(store)
    pending = grant_store.pending(now=datetime.now(UTC))
    if not pending:
        typer.echo("no pending authorization requests")
        return
    for entry in pending:
        typer.echo(f"{entry.request_id}   {entry.command}   S3   requested {entry.requested_at}")
        for name, value in sorted(entry.params.items()):
            typer.echo(f"  {name:8} {json.dumps(value)}")
        typer.echo(f"  digest   {entry.params_digest}")


@grant_app.command("approve")
def grant_approve(
    request_id: str,
    store: Annotated[Path | None, typer.Option("--store", help="Grant store directory")] = None,
    ttl: Annotated[str, typer.Option("--ttl", help="Validity window, e.g. 15m, 2h")] = "15m",
    uses: Annotated[int, typer.Option("--uses", help="Maximum number of uses")] = 1,
    issued_by: Annotated[str | None, typer.Option("--issued-by", help="Free-text label")] = None,
    note: Annotated[str | None, typer.Option("--note", help="Free-text note")] = None,
) -> None:
    """Approve one pending request, minting a grant bound to its exact parameters."""
    import re
    from datetime import UTC, datetime, timedelta

    match = re.fullmatch(r"(\d+)([smh])", ttl)
    if match is None:
        typer.echo(f"error: --ttl {ttl!r} is not like 15m / 90s / 2h", err=True)
        raise typer.Exit(2)
    seconds = int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    grant_store = _store(store)
    try:
        grant = grant_store.approve(
            request_id,
            now=datetime.now(UTC),
            ttl=timedelta(seconds=seconds),
            max_uses=uses,
            issued_by=issued_by,
            note=note,
        )
    except KeyError as exc:
        typer.echo(f"error: {exc.args[0]}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"grant {grant.grant_id}  uses 0/{grant.max_uses}  expires {grant.expires_at}")


@grant_app.command("revoke")
def grant_revoke(
    grant_id: str,
    store: Annotated[Path | None, typer.Option("--store", help="Grant store directory")] = None,
) -> None:
    """Revoke a grant; presenting it afterwards is refused as revoked."""
    if _store(store).revoke(grant_id):
        typer.echo(f"revoked {grant_id}")
    else:
        typer.echo(f"error: no grant {grant_id!r}", err=True)
        raise typer.Exit(1)


def main() -> None:
    """Console-script entry point."""
    app()
