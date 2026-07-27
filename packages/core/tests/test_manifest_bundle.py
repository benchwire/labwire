"""End-to-end: signed bundles written per terminal run (SPEC §12)."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TypedDict

import pytest
from labwire.core import (
    CommandContext,
    HardwareFaultError,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    LabwireClient,
    MemoryTransport,
    channel,
    command,
)
from labwire.core.signing import Manifest, verify_bundle, verify_manifest
from pydantic import ConfigDict


class HeatResult(TypedDict):
    """A closed result schema for this test instrument."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]

    reached_c: float


class Reactor(Instrument):
    """Streams temperature while heating; can fail on demand."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="SimReactor-1",
        serial_number="SIM-0011",
        firmware_version="0.1.0",
    )

    temperature = channel("temperature", unit="Cel", description="Vessel temperature.")

    @command(units={"target_c": "Cel"}, returns_units={"reached_c": "Cel"})
    async def heat(self, ctx: CommandContext, target_c: float) -> HeatResult:
        """Ramp to a target temperature, streaming readings."""
        for step in (0.5, 0.9, 1.0):
            self.temperature.publish(target_c * step)
            ctx.emit_event("x-sim/ramp", "info", {"fraction": step})
            await asyncio.sleep(0)
        return {"reached_c": target_c}

    @command()
    async def melt_down(self, ctx: CommandContext) -> None:
        """Fail with a hardware fault."""
        raise HardwareFaultError("thermocouple detached")


@pytest.fixture
async def signed_rig(tmp_path: Path) -> AsyncIterator[tuple[Path, LabwireClient]]:
    server = InstrumentServer(Reactor(), manifest_dir=tmp_path / "runs")
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        yield tmp_path / "runs", client
    await server.aclose()


async def test_manifests_capability_advertised(
    signed_rig: tuple[Path, LabwireClient],
) -> None:
    _runs, client = signed_rig
    assert client.capabilities is not None
    assert client.capabilities.manifests is True


async def test_unsigned_server_does_not_advertise_manifests() -> None:
    server = InstrumentServer(Reactor())
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        assert client.capabilities is not None
        assert client.capabilities.manifests is False
    await server.aclose()


async def test_successful_run_writes_verifiable_bundle(
    signed_rig: tuple[Path, LabwireClient],
) -> None:
    runs, client = signed_rig
    handle = await client.submit("heat", {"target_c": 80.0})
    result = await handle.result(timeout=5.0)
    assert result == {"reached_c": 80.0}
    bundle = runs / handle.command_id
    doc = json.loads((bundle / "manifest.json").read_text())
    manifest = Manifest.model_validate(doc)
    assert manifest.status == "succeeded"
    assert manifest.result == {"reached_c": 80.0}
    assert manifest.command.params == {"target_c": 80.0}
    assert manifest.data.channels == ["temperature"]
    assert manifest.instrument.model == "SimReactor-1"
    assert set(manifest.timestamps.model_dump()) >= {"submitted", "started", "completed"}
    # the signature verifies, and the record stream reproduces the digest
    assert verify_manifest(doc).ok
    records = (bundle / "records.jsonl").read_bytes()
    assert hashlib.sha256(records).hexdigest() == manifest.data.digest
    lines = [json.loads(line) for line in records.splitlines()]
    kinds = {line["type"] for line in lines}
    assert kinds == {"sample", "event"}
    outcome = verify_bundle(bundle)
    assert outcome.ok, outcome.errors


async def test_failed_run_manifest_records_the_error(
    signed_rig: tuple[Path, LabwireClient],
) -> None:
    runs, client = signed_rig
    handle = await client.submit("melt_down", {})
    with pytest.raises(HardwareFaultError):
        await handle.result(timeout=5.0)
    doc = json.loads((runs / handle.command_id / "manifest.json").read_text())
    manifest = Manifest.model_validate(doc)
    assert manifest.status == "failed"
    assert manifest.error is not None
    assert manifest.error.data is not None
    assert manifest.error.data.category == "hardware_fault"
    assert verify_bundle(runs / handle.command_id).ok


async def test_tampered_bundle_fails_verification(
    signed_rig: tuple[Path, LabwireClient],
) -> None:
    runs, client = signed_rig
    handle = await client.submit("heat", {"target_c": 50.0})
    await handle.result(timeout=5.0)
    bundle = runs / handle.command_id
    doc = json.loads((bundle / "manifest.json").read_text())
    doc["result"]["reached_c"] = 9000.0  # falsify the outcome
    (bundle / "manifest.json").write_text(json.dumps(doc))
    outcome = verify_bundle(bundle)
    assert not outcome.ok
    assert any("signature" in e for e in outcome.errors)


async def test_signing_key_persists_across_servers(tmp_path: Path) -> None:
    key_ids: list[str] = []
    for _ in range(2):
        server = InstrumentServer(Reactor(), manifest_dir=tmp_path / "runs")
        client_end, server_end = MemoryTransport.pair()
        server.attach(server_end)
        async with LabwireClient.attach(client_end) as client:
            handle = await client.submit("heat", {"target_c": 10.0})
            await handle.result(timeout=5.0)
            doc = json.loads((tmp_path / "runs" / handle.command_id / "manifest.json").read_text())
            key_ids.append(doc["signer"]["key_id"])
        await server.aclose()
    assert key_ids[0] == key_ids[1]  # trust-on-first-use: same key across restarts
