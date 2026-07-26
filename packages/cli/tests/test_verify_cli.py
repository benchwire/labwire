"""Tests for `labwire verify` against real signed bundles."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from labwire.core import (
    CommandContext,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    LabwireClient,
    MemoryTransport,
    command,
)


class Clicker(Instrument):
    """Trivial instrument producing signed runs."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="SimClicker-1",
        serial_number="SIM-0021",
        firmware_version="0.1.0",
    )

    @command(units={"times": "1"}, returns_units={"clicked": "1"})
    async def click(self, ctx: CommandContext, times: int = 1) -> dict[str, int]:
        """Click a relay."""
        ctx.emit_event("x-sim/click", "info", {"times": times})
        return {"clicked": times}


@pytest.fixture
async def bundle(tmp_path: Path) -> Path:
    server = InstrumentServer(Clicker(), manifest_dir=tmp_path / "runs")
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        handle = await client.submit("click", {"times": 3})
        await handle.result(timeout=5.0)
    await server.aclose()
    return tmp_path / "runs" / handle.command_id


def _verify(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "labwire.cli", "verify", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_verify_accepts_an_authentic_bundle(bundle: Path) -> None:
    proc = _verify(bundle)
    assert proc.returncode == 0, proc.stderr
    assert "OK: run" in proc.stdout
    assert "SimClicker-1" in proc.stdout
    assert "signed by:  sha256:" in proc.stdout


def test_verify_rejects_a_tampered_bundle(bundle: Path) -> None:
    manifest_path = bundle / "manifest.json"
    doc = json.loads(manifest_path.read_text())
    doc["result"]["clicked"] = 9999
    manifest_path.write_text(json.dumps(doc))
    proc = _verify(bundle)
    assert proc.returncode == 1
    assert "FAILED" in proc.stderr


def test_verify_rejects_tampered_records(bundle: Path) -> None:
    records = bundle / "records.jsonl"
    records.write_bytes(records.read_bytes().replace(b'"times":3', b'"times":4'))
    proc = _verify(bundle)
    assert proc.returncode == 1
    assert "digest" in proc.stderr


def test_verify_reports_missing_manifest(tmp_path: Path) -> None:
    proc = _verify(tmp_path / "nope")
    assert proc.returncode == 1
