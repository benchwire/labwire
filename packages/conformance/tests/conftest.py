"""A fixture instrument that makes every conformance check applicable."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, cast

import pytest
from labwire.core import (
    CommandContext,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    ResourceIndexEntry,
    ResourceSnapshot,
    channel,
    command,
    resource,
    unit_field,
)
from pydantic import BaseModel, ConfigDict, Field

CONFIRMATION = "conformance-fixture-standing-grant"


def _rack_ref(kind: str) -> type[str]:
    return Annotated[  # pyright: ignore[reportReturnType]
        str,
        Field(json_schema_extra={"resource_ref": {"kind": kind, "enumerated_by": "labwire:rack"}}),
    ]


Slot = _rack_ref("site")


class RackState(BaseModel):
    """Occupancy of the rack."""

    model_config = ConfigDict(extra="forbid")

    occupied: list[str]
    capacity: int = unit_field("1")


class Reading(BaseModel):
    """One temperature reading."""

    model_config = ConfigDict(extra="forbid")

    temperature_c: float


class ConformanceRig(Instrument):
    """Declares every protocol feature the suite can check."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="ConformanceRig-1",
        serial_number="SIM-0090",
        firmware_version="0.3.0",
    )

    temperature = channel("temperature", unit="Cel", description="Block temperature.")

    rack = resource(
        "labwire:rack",
        kind="deck",
        title="Rack",
        description="Slot occupancy; every slot a command can name.",
        content_model=RackState,
        item_kinds=["site"],
    )

    def __init__(self) -> None:
        super().__init__()
        self.slots: dict[str, str] = {}
        self.slot_ids = ["S1", "S2", "S3"]

    @rack.reader
    def _read_rack(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            index=[
                ResourceIndexEntry(
                    uri="labwire:rack",
                    kinds=["deck"],
                    children={"kinds": ["site"], "ids": list(self.slot_ids)},  # pyright: ignore[reportArgumentType]
                )
            ],
            content=RackState(occupied=sorted(self.slots), capacity=len(self.slot_ids)),
        )

    @command(units={"settle_s": "s"}, returns_units={"temperature_c": "Cel"})
    async def measure(self, ctx: CommandContext, settle_s: float) -> Reading:
        """Read the block temperature after settling."""
        self.temperature.publish(21.5)  # so the run's bundle carries records
        await ctx.sleep(0.0)
        return Reading(temperature_c=21.5)

    @command(safety_class="S2")
    async def store(self, ctx: CommandContext, slot: Slot, barcode: str) -> None:  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType]
        """Store a plate in a slot. Irreversible enough to be S2."""
        self.slots[str(cast("str", slot)).rsplit("/", 1)[1]] = barcode
        self.rack.touch()

    @command(safety_class="S3")
    async def purge(self, ctx: CommandContext, slot: Slot) -> None:  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType]
        """Discard whatever a slot holds. Destroys a sample."""
        self.slots.pop(str(cast("str", slot)).rsplit("/", 1)[1], None)
        self.rack.touch()


@pytest.fixture
async def rig_url(tmp_path: Path) -> AsyncIterator[tuple[str, Path]]:
    """The rig served over a real WebSocket; yields (url, manifest_dir)."""
    manifest_dir = tmp_path / "runs"
    server = InstrumentServer(
        ConformanceRig(),
        confirmation_token=CONFIRMATION,
        grant_store=tmp_path / "grants",
        manifest_dir=manifest_dir,
    )
    async with server.serve_websocket("127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}", manifest_dir
    await server.aclose()
