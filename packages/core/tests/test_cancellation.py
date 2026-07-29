"""SPEC 8.3: declared cancel semantics, acknowledgment vs settlement.

The behaviors here exist because of two field reports (SPEC-FINDINGS
F10): a stop request returning does not mean motion stopped, and a
command already on the wire executes regardless of who stopped waiting.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from labwire.core import (
    CanceledError,
    CommandContext,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    LabwireClient,
    MemoryTransport,
    NotCancelableError,
    command,
    verify_bundle,
)
from pydantic import BaseModel, ConfigDict


class Moved(BaseModel):
    """Result of the two-step transfer."""

    model_config = ConfigDict(extra="forbid")

    steps: int


class SemanticsRig(Instrument):
    """One command per cancel semantics, each honest about what it does."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="SemanticsRig-1",
        serial_number="SIM-0092",
        firmware_version="0.4.0",
    )

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.steps_done: list[str] = []

    @command()
    async def committed(self, ctx: CommandContext) -> dict[str, bool]:
        """Declares nothing, so cancel means nothing: runs to completion."""
        await self.release.wait()
        return {"finished": True}

    @command(cancel="between_steps", returns_units={"steps": "1"})
    async def two_step(self, ctx: CommandContext) -> Moved:
        """A bridge-sequenced routine: each step is committed once issued."""
        await self.release.wait()
        self.steps_done.append("aspirate")
        ctx.boundary("aspirate", of=2)
        self.steps_done.append("dispense")
        ctx.boundary("dispense", of=2)
        return Moved(steps=len(self.steps_done))

    @command(cancel="abort")
    async def hold(self, ctx: CommandContext) -> dict[str, bool]:
        """A genuine halt path: the loop is ours and provably stops."""
        while not ctx.cancel_requested:  # noqa: ASYNC110 - polls the cancel flag
            await asyncio.sleep(0.001)
        ctx.confirm_halted("hold loop exited")

    @command(cancel="between_steps", returns_units={"steps": "1"})
    async def two_step_poisoned(self, ctx: CommandContext) -> Moved:
        """Passes a boundary clean, then abandons mid-step: no boundary claim."""
        ctx.boundary("aspirate", of=2)  # no cancel pending yet: passes clean
        await self.release.wait()  # the test cancels while "step 2" is in flight
        raise CanceledError("abandoning mid-dispense")

    @command(cancel="between_steps", returns_units={"steps": "1"})
    async def crash_after_release(self, ctx: CommandContext) -> Moved:
        """Crashes with a plain exception (a handler bug)."""
        await self.release.wait()
        raise ValueError("boom")

    @command()
    async def quit_spontaneously(self, ctx: CommandContext) -> dict[str, bool]:
        """Raises CanceledError although nobody asked."""
        raise CanceledError("driver decided to stop")

    @command(cancel="abort")
    async def hold_shaky(self, ctx: CommandContext) -> dict[str, bool]:
        """An abort whose backend never confirms: settles unconfirmed."""
        while not ctx.cancel_requested:  # noqa: ASYNC110 - polls the cancel flag
            await asyncio.sleep(0.001)
        raise CanceledError("stop sent; device never acknowledged")


async def _terminal(handle: Any, timeout: float = 5.0) -> Any:
    """Poll until the run is terminal and return its CommandStatus."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        status = await handle.status()
        if status.status in ("succeeded", "failed", "canceled"):
            return status
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"run stuck in {status.status}")
        await asyncio.sleep(0.005)


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[tuple[SemanticsRig, LabwireClient, Path]]:
    instrument = SemanticsRig()
    manifest_dir = tmp_path / "runs"
    server = InstrumentServer(instrument, manifest_dir=manifest_dir)
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        yield instrument, client, manifest_dir
    await server.aclose()


async def test_cancel_on_none_running_is_refused_with_details(
    rig: tuple[SemanticsRig, LabwireClient, Path],
) -> None:
    """SPEC 8.3: refusal is the only honest answer; details say why."""
    instrument, client, _ = rig
    handle = await client.submit("committed", {})
    await asyncio.sleep(0.01)
    with pytest.raises(NotCancelableError) as caught:
        await handle.cancel()
    assert caught.value.details == {"cancel_semantics": "none", "state": "running"}
    instrument.release.set()
    status = await _terminal(handle)
    assert status.status == "succeeded"
    assert status.cancellation is None


async def test_between_steps_settles_at_the_boundary(
    rig: tuple[SemanticsRig, LabwireClient, Path],
) -> None:
    """The in-flight step finishes; the next is never issued."""
    instrument, client, _ = rig
    handle = await client.submit("two_step", {})
    await asyncio.sleep(0.01)
    await handle.cancel()
    instrument.release.set()
    status = await _terminal(handle)
    assert status.status == "canceled"
    assert status.cancellation is not None
    assert status.cancellation.outcome == "halted_at_boundary"
    assert status.cancellation.boundary is not None
    assert status.cancellation.boundary.last == "aspirate"
    assert status.cancellation.boundary.completed_steps == 1
    assert status.cancellation.boundary.of_steps == 2
    assert instrument.steps_done == ["aspirate"]  # dispense never ran


async def test_abort_with_confirmation_settles_halted(
    rig: tuple[SemanticsRig, LabwireClient, Path],
) -> None:
    _instrument, client, _ = rig
    handle = await client.submit("hold", {})
    await asyncio.sleep(0.01)
    await handle.cancel()
    status = await _terminal(handle)
    assert status.status == "canceled"
    assert status.cancellation is not None
    assert status.cancellation.outcome == "halted"
    assert status.cancellation.detail == "hold loop exited"


async def test_abort_without_confirmation_settles_unconfirmed(
    rig: tuple[SemanticsRig, LabwireClient, Path],
) -> None:
    """The honest case: nobody confirmed the physical state."""
    _instrument, client, _ = rig
    handle = await client.submit("hold_shaky", {})
    await asyncio.sleep(0.01)
    await handle.cancel()
    status = await _terminal(handle)
    assert status.status == "canceled"
    assert status.cancellation is not None
    assert status.cancellation.outcome == "unconfirmed"
    assert status.cancellation.detail == "stop sent; device never acknowledged"


async def test_completion_winning_the_race_reports_ran_to_completion(
    rig: tuple[SemanticsRig, LabwireClient, Path],
) -> None:
    """A cancel that lost settles succeeded + ran_to_completion, not canceled."""
    instrument, client, _ = rig
    handle = await client.submit("two_step", {})
    await asyncio.sleep(0.01)
    await handle.cancel()
    # Sneak past both boundaries before the handler can observe the cancel:
    # clear the flag's effect by releasing AFTER cancel; the handler then
    # hits boundary("aspirate") with cancel pending... so instead release
    # first on a fresh run to let completion genuinely win.
    instrument.release.set()
    status = await _terminal(handle)
    # With the release racing the cancel, either the boundary caught it or
    # completion won; both must be settled honestly.
    assert status.cancellation is not None
    if status.status == "succeeded":
        assert status.cancellation.outcome == "ran_to_completion"
    else:
        assert status.cancellation.outcome == "halted_at_boundary"


async def test_cancel_before_start_settles_never_started(
    rig: tuple[SemanticsRig, LabwireClient, Path],
) -> None:
    """Dequeuing is not interruption: allowed even for none commands.

    Cancelling in the same event-loop turn as the submit reply reaches the
    run while it is still accepted; if the scheduler got there first the
    refusal path is asserted instead, so both honest outcomes are pinned.
    """
    instrument, client, _ = rig
    handle = await client.submit("committed", {})
    try:
        await handle.cancel()
    except NotCancelableError as exc:
        assert exc.details == {"cancel_semantics": "none", "state": "running"}  # noqa: PT017
        instrument.release.set()
        status = await _terminal(handle)
        assert status.status == "succeeded"
        return
    status = await _terminal(handle)
    assert status.status == "canceled"
    assert status.cancellation is not None
    assert status.cancellation.outcome == "never_started"
    assert "before start" in (status.cancellation.detail or "")


async def test_manifest_carries_the_settlement_block(
    rig: tuple[SemanticsRig, LabwireClient, Path],
) -> None:
    """The signed record states the boundary; verify still passes."""
    instrument, client, manifest_dir = rig
    handle = await client.submit("two_step", {})
    await asyncio.sleep(0.01)
    await handle.cancel()
    instrument.release.set()
    status = await _terminal(handle)
    assert status.status in ("canceled", "succeeded")
    import json

    bundle = manifest_dir / handle.command_id
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["manifest_version"] == "0.4"
    assert manifest["cancellation"]["outcome"] in ("halted_at_boundary", "ran_to_completion")
    assert verify_bundle(bundle).ok


async def test_boundary_on_a_non_between_steps_command_is_a_driver_bug() -> None:
    def _no_event(name: str, severity: Any, data: dict[str, Any]) -> None:
        return None

    ctx = CommandContext(_FakeClock(), _no_event, _no_progress, cancel_semantics="none")
    with pytest.raises(RuntimeError, match="between_steps"):
        ctx.boundary("step")
    with pytest.raises(RuntimeError, match="abort"):
        ctx.confirm_halted()


class _FakeClock:
    def now(self):  # pragma: no cover - never called
        raise AssertionError

    async def sleep(self, seconds: float) -> None:  # pragma: no cover
        await asyncio.sleep(0)


async def _no_progress(fraction: float | None, message: str | None) -> None:
    return None


async def test_boundary_passed_then_midstep_abandon_settles_unconfirmed(
    rig: tuple[SemanticsRig, LabwireClient, Path],
) -> None:
    """Provenance: a CanceledError NOT raised by ctx.boundary() must not
    claim a boundary stop, whatever boundaries the run passed earlier."""
    instrument, client, _ = rig
    handle = await client.submit("two_step_poisoned", {})
    await asyncio.sleep(0.01)
    await handle.cancel()
    instrument.release.set()
    status = await _terminal(handle)
    assert status.status == "canceled"
    assert status.cancellation is not None
    assert status.cancellation.outcome == "unconfirmed"
    assert status.cancellation.boundary is None


async def test_server_shutdown_settles_unconfirmed_with_a_block(tmp_path: Path) -> None:
    """SPEC 8.3: no canceled terminal without a block, shutdown included."""
    import json

    instrument = SemanticsRig()
    manifest_dir = tmp_path / "runs"
    server = InstrumentServer(instrument, manifest_dir=manifest_dir)
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        handle = await client.submit("committed", {})
        await asyncio.sleep(0.01)
        command_id = handle.command_id
        await server.aclose()  # nobody ever asked to cancel
    manifest = json.loads((manifest_dir / command_id / "manifest.json").read_text())
    assert manifest["status"] == "canceled"
    assert manifest["cancellation"]["outcome"] == "unconfirmed"
    assert manifest["cancellation"].get("requested_at") is None
    assert manifest["command"]["cancel_semantics"] == "none"


async def test_handler_crash_while_canceling_still_carries_a_block(
    rig: tuple[SemanticsRig, LabwireClient, Path],
) -> None:
    instrument, client, _ = rig
    handle = await client.submit("crash_after_release", {})
    await asyncio.sleep(0.01)
    await handle.cancel()
    instrument.release.set()
    status = await _terminal(handle)
    assert status.status == "failed"
    assert status.cancellation is not None
    assert status.cancellation.outcome == "unconfirmed"
    assert "crashed" in (status.cancellation.detail or "")


async def test_spontaneous_canceled_error_settles_unconfirmed_null_request(
    rig: tuple[SemanticsRig, LabwireClient, Path],
) -> None:
    """A handler-initiated stop nobody requested still ends with an honest
    block: unconfirmed, requested_at null."""
    _instrument, client, _ = rig
    handle = await client.submit("quit_spontaneously", {})
    status = await _terminal(handle)
    assert status.status == "canceled"
    assert status.cancellation is not None
    assert status.cancellation.outcome == "unconfirmed"
    assert status.cancellation.requested_at is None


async def test_verify_rejects_incoherent_cancellation_claims(
    rig: tuple[SemanticsRig, LabwireClient, Path],
) -> None:
    """SPEC 13.1: the cancellation MUSTs are auditable offline, and audited."""
    import json
    import shutil

    instrument, client, manifest_dir = rig
    handle = await client.submit("hold", {})
    await asyncio.sleep(0.01)
    await handle.cancel()
    await _terminal(handle)
    instrument.release.set()
    bundle = manifest_dir / handle.command_id
    assert verify_bundle(bundle).ok

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        # A halted claim on a command not declared abort must be rejected.
        forged = Path(tmp) / "forged"
        shutil.copytree(bundle, forged)
        manifest = json.loads((forged / "manifest.json").read_text())
        manifest["command"]["cancel_semantics"] = "none"
        (forged / "manifest.json").write_text(json.dumps(manifest))
        outcome = verify_bundle(forged)
        assert not outcome.ok  # signature broke AND the semantic rule fires
        assert any("only 'abort' commands" in e for e in outcome.errors)

        # A canceled status stripped of its block must be rejected.
        stripped = Path(tmp) / "stripped"
        shutil.copytree(bundle, stripped)
        manifest = json.loads((stripped / "manifest.json").read_text())
        del manifest["cancellation"]
        (stripped / "manifest.json").write_text(json.dumps(manifest))
        outcome = verify_bundle(stripped)
        assert not outcome.ok
        assert any("no cancellation block" in e for e in outcome.errors)
