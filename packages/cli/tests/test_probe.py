"""``labwire probe`` against the SCPI simulator over a real TCP socket."""

import asyncio
from pathlib import Path

from labwire.cli.main import app
from labwire.cli.probe import parse_idn, slug
from labwire.sim import SimPowerSupply
from typer.testing import CliRunner

runner = CliRunner()


async def test_probe_drafts_an_annotation_from_a_live_idn(tmp_path: Path) -> None:
    sim = SimPowerSupply()
    await sim.start()
    out = tmp_path / "draft.yaml"
    try:
        result = await asyncio.to_thread(
            runner.invoke, app, ["probe", f"127.0.0.1:{sim.port}", "--out", str(out)]
        )
    finally:
        await sim.stop()
    assert result.exit_code == 0, result.output
    draft = out.read_text()
    assert "model: 'SimPSU-3005'" in draft
    assert "manufacturer: 'Labwire Project'" in draft
    assert "driver: TODO" in draft
    assert "no labwire driver claims" in draft
    assert "transport: tcp" in draft


async def test_probe_refuses_ambiguous_endpoints() -> None:
    result = await asyncio.to_thread(runner.invoke, app, ["probe"])
    assert result.exit_code == 1
    assert "exactly one endpoint" in result.output


async def test_probe_reports_a_dead_endpoint(tmp_path: Path) -> None:
    result = await asyncio.to_thread(runner.invoke, app, ["probe", "127.0.0.1:1"])
    assert result.exit_code == 1
    assert "probe failed" in result.output


def test_idn_parsing_pads_missing_fields_with_todo() -> None:
    assert parse_idn("ACME,PS-1") == {
        "manufacturer": "ACME",
        "model": "PS-1",
        "serial_number": "TODO",
        "firmware_version": "TODO",
    }


def test_slug_makes_safe_filenames() -> None:
    assert slug("SimPSU-3005") == "SimPSU-3005"
    assert slug("weird/model *name") == "weird-model-name"
    assert slug("///") == "instrument"
