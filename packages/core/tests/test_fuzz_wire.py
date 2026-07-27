"""Property-based fuzzing of the wire layer (hypothesis).

Properties, not examples: whatever arrives, the server session survives and
keeps answering; whatever is flipped in a signed bundle, verification says
no and never crashes. Deterministic in CI (derandomize) so a red run is
reproducible; surprises graduate to SPEC-FINDINGS.
"""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from labwire.core import (
    CommandContext,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    MemoryTransport,
    channel,
    command,
    verify_bundle,
)
from pydantic import BaseModel, ConfigDict

FUZZ = settings(
    max_examples=60,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


class Echo(BaseModel):
    """The no-op result."""

    model_config = ConfigDict(extra="forbid")

    volume_ul: float


class FuzzTarget(Instrument):
    """A harmless instrument: nothing here touches anything."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="FuzzTarget-1",
        serial_number="SIM-0091",
        firmware_version="0.3.0",
    )

    flow = channel("flow", unit="uL/s", description="Streamed so bundles carry records.")

    @command(units={"volume_ul": "uL"}, returns_units={"volume_ul": "uL"})
    async def echo(self, ctx: CommandContext, volume_ul: float) -> Echo:
        """Return the volume unchanged."""
        self.flow.publish(volume_ul)  # so the run's bundle has records to tamper
        return Echo(volume_ul=volume_ul)


_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=10), children, max_size=4)
    ),
    max_leaves=12,
)

_envelopes = st.dictionaries(
    st.sampled_from(["jsonrpc", "id", "method", "params", "result", "error", "junk"]),
    _json_values,
    max_size=6,
)


async def _survives(garbage: dict[str, Any]) -> None:
    server = InstrumentServer(FuzzTarget())
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    try:
        await client_end.send(garbage)
        await client_end.send({"jsonrpc": "2.0", "id": 777, "method": "ping", "params": {}})
        while True:
            reply = await asyncio.wait_for(client_end.receive(), timeout=5.0)
            if reply.get("id") == 777:
                assert "result" in reply, f"ping failed after garbage: {reply}"
                break
    finally:
        await server.aclose()


@FUZZ
@given(garbage=_envelopes)
def test_arbitrary_envelopes_never_kill_the_session(garbage: dict[str, Any]) -> None:
    """Whatever one frame contains, the next ping still answers."""
    asyncio.run(_survives(garbage))


@FUZZ
@given(params=_json_values)
def test_arbitrary_submit_params_get_an_answer(params: Any) -> None:
    """command/submit with any params answers: a result or a taxonomy error."""

    async def run() -> None:
        server = InstrumentServer(FuzzTarget())
        client_end, server_end = MemoryTransport.pair()
        server.attach(server_end)
        try:
            await client_end.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocol_version": "0.3",
                        "client_info": {"name": "fuzz", "version": "0"},
                        "capabilities": {},
                    },
                }
            )
            await client_end.send(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            )
            await client_end.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "command/submit",
                    "params": {"command": "echo", "params": params},
                }
            )
            seen = False
            while not seen:
                reply = await asyncio.wait_for(client_end.receive(), timeout=5.0)
                if reply.get("id") == 2:
                    seen = True
                    if "error" in reply:
                        data: dict[str, Any] = reply["error"].get("data") or {}
                        assert "category" in data, f"error without taxonomy category: {reply}"
        finally:
            await server.aclose()

    asyncio.run(run())


def _deep(depth: int) -> Any:
    payload: Any = 0
    for _ in range(depth):
        payload = {"p": payload}
    return payload


def test_a_pathologically_deep_params_payload_is_answered_or_refused() -> None:
    """A 500-level-deep params object must not kill the session."""
    asyncio.run(
        _survives(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "command/submit",
                "params": {"command": "echo", "params": _deep(500)},
            }
        )
    )


# --- signed bundle tampering ------------------------------------------------


def _make_bundle() -> Path:
    """One real signed bundle from one real run, built once."""

    async def run() -> Path:
        manifest_dir = Path(tempfile.mkdtemp(prefix="labwire-fuzz-bundle-"))
        server = InstrumentServer(FuzzTarget(), manifest_dir=manifest_dir)
        client_end, server_end = MemoryTransport.pair()
        server.attach(server_end)
        from labwire.core import LabwireClient

        async with LabwireClient.attach(client_end) as client:
            handle = await client.submit("echo", {"volume_ul": 10.0})
            await handle.result(timeout=10.0)
            bundle = manifest_dir / handle.command_id
        await server.aclose()
        assert (bundle / "manifest.json").exists()
        return bundle

    return asyncio.run(run())


_bundle_cache: Path | None = None


def _bundle() -> Path:
    global _bundle_cache
    if _bundle_cache is None:
        _bundle_cache = _make_bundle()
    assert verify_bundle(_bundle_cache).ok
    return _bundle_cache


@FUZZ
@given(data=st.data())
def test_any_content_changing_byte_flip_breaks_verification(data: st.DataObject) -> None:
    """Flip any byte of manifest.json: verify says no, or the flip changed
    nothing the signature covers (formatting-only), and it never crashes.

    Signatures bind RFC 8785 canonical content, not raw bytes, so a flip
    inside insignificant whitespace legitimately still verifies.
    """
    source = _bundle()
    original = json.loads((source / "manifest.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        copied = Path(tmp) / "bundle"
        shutil.copytree(source, copied)
        target = copied / "manifest.json"
        content = bytearray(target.read_bytes())
        offset = data.draw(st.integers(0, len(content) - 1))
        flip = data.draw(st.integers(1, 255))
        content[offset] ^= flip
        target.write_bytes(bytes(content))
        try:
            unchanged = json.loads(bytes(content)) == original
        except ValueError:
            unchanged = False
        outcome = verify_bundle(copied)
        if unchanged:
            assert outcome.ok, f"formatting-only flip at {offset} was rejected"
        else:
            assert not outcome.ok, f"byte {offset} xor {flip} still verifies"


@FUZZ
@given(data=st.data())
def test_any_records_truncation_breaks_verification(data: st.DataObject) -> None:
    """Cut records.jsonl anywhere: the data digest must not match."""
    source = _bundle()
    records = (source / "records.jsonl").read_bytes()
    if not records:
        return
    with tempfile.TemporaryDirectory() as tmp:
        copied = Path(tmp) / "bundle"
        shutil.copytree(source, copied)
        cut = data.draw(st.integers(0, len(records) - 1))
        (copied / "records.jsonl").write_bytes(records[:cut])
        assert not verify_bundle(copied).ok, f"truncation at {cut} still verifies"


def test_a_bundle_with_garbage_manifest_reports_instead_of_crashing() -> None:
    """verify_bundle on unparseable JSON returns a verdict, not a traceback."""
    source = _bundle()
    with tempfile.TemporaryDirectory() as tmp:
        copied = Path(tmp) / "bundle"
        shutil.copytree(source, copied)
        (copied / "manifest.json").write_text("not json {")
        outcome = verify_bundle(copied)
        assert not outcome.ok


def test_an_invalid_utf8_manifest_reports_instead_of_crashing() -> None:
    """A flip that breaks UTF-8 decoding still gets a verdict."""
    source = _bundle()
    with tempfile.TemporaryDirectory() as tmp:
        copied = Path(tmp) / "bundle"
        shutil.copytree(source, copied)
        content = bytearray((copied / "manifest.json").read_bytes())
        content[10] = 0xFF
        (copied / "manifest.json").write_bytes(bytes(content))
        outcome = verify_bundle(copied)
        assert not outcome.ok
        assert "unreadable manifest" in outcome.errors[0]
