"""Resources and typed references (SPEC §7.6, §10): the v0.3 primitives."""

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from labwire.core import (
    AuthorizationRequiredError,
    CommandContext,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    LabwireClient,
    MemoryTransport,
    StaleRevisionError,
    UnknownReferenceError,
    command,
)
from labwire.core.messages import ResourceIndexEntry
from labwire.core.server import ResourceSnapshot, resource, unit_field
from pydantic import BaseModel, ConfigDict

GRANT = "hotel-operator-grant"


class SlotContents(BaseModel):
    """One occupied slot of the plate hotel."""

    model_config = ConfigDict(extra="forbid")

    uri: str
    plate_barcode: str
    stored_minutes: float = unit_field("min")


class HotelState(BaseModel):
    """The hotel's content model: occupied slots only, sparse."""

    model_config = ConfigDict(extra="forbid")

    occupied: list[SlotContents]
    capacity: int = unit_field("1")


def _hotel_ref(kind: str) -> type[str]:
    from typing import Annotated

    from pydantic import Field

    return Annotated[  # pyright: ignore[reportReturnType]
        str,
        Field(json_schema_extra={"resource_ref": {"kind": kind, "enumerated_by": "labwire:hotel"}}),
    ]


Slot = _hotel_ref("site")


class PlateHotel(Instrument):
    """A storage hotel: slots are sites, and store/retrieve reference them."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="HotelRig-1",
        serial_number="SIM-0051",
        firmware_version="0.3.0",
    )

    hotel = resource(
        "labwire:hotel",
        kind="deck",
        title="Hotel",
        description="Slot occupancy. Every slot a command can name is in this index.",
        content_model=HotelState,
        item_kinds=["site"],
    )

    def __init__(self) -> None:
        super().__init__()
        self.slots: dict[str, str] = {}  # slot id -> barcode
        self.slot_ids = ["S1", "S2", "S3"]

    @hotel.reader
    def _read_hotel(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            index=[
                ResourceIndexEntry(
                    uri="labwire:hotel",
                    kinds=["deck"],
                    children={"kinds": ["site"], "ids": list(self.slot_ids)},  # pyright: ignore[reportArgumentType]
                )
            ],
            content=HotelState(
                occupied=[
                    SlotContents(
                        uri=f"labwire:hotel/{slot}", plate_barcode=code, stored_minutes=1.0
                    )
                    for slot, code in sorted(self.slots.items())
                ],
                capacity=len(self.slot_ids),
            ),
        )

    @command(safety_class="S2")
    async def store(self, ctx: CommandContext, slot: Slot, barcode: str) -> None:  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType]
        """Store a plate in a slot."""
        self.slots[str(cast("str", slot)).rsplit("/", 1)[1]] = barcode
        self.hotel.touch()

    @command(safety_class="S3")
    async def purge(self, ctx: CommandContext, slot: Slot) -> None:  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType]
        """Discard whatever a slot holds. Destroys a sample."""
        self.slots.pop(str(cast("str", slot)).rsplit("/", 1)[1], None)
        self.hotel.touch()


@pytest.fixture
async def hotel() -> AsyncIterator[tuple[PlateHotel, InstrumentServer, LabwireClient]]:
    instrument = PlateHotel()
    server = InstrumentServer(
        instrument,
        confirmation_token=GRANT,
        grant_store=Path(tempfile.mkdtemp(prefix="labwire-grants-")),
    )
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        yield instrument, server, client
    await server.aclose()


# --- declaration ------------------------------------------------------------


async def test_the_descriptor_declares_the_resource(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = hotel
    descriptor = await client.describe()
    assert len(descriptor.resources) == 1
    declared = descriptor.resources[0]
    assert declared.uri == "labwire:hotel"
    assert declared.item_kinds == ["site"]
    assert declared.revision  # a live snapshot, not a placeholder


async def test_the_capability_flag_advertises_resources(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = hotel
    assert client.capabilities is not None
    assert client.capabilities.resources is True
    assert client.capabilities.grants is True


def test_numeric_content_without_a_unit_keyword_is_refused() -> None:
    """SPEC 7.6: a units-optional state format would reopen F5."""

    class Bare(BaseModel):
        model_config = ConfigDict(extra="forbid")
        volume: float  # no unit keyword

    with pytest.raises(TypeError, match="unit"):
        resource(
            "labwire:x",
            kind="consumable",
            title="X",
            description="X.",
            content_model=Bare,
        )


def test_an_unregistered_kind_is_refused() -> None:
    class Fine(BaseModel):
        model_config = ConfigDict(extra="forbid")
        label: str

    with pytest.raises(TypeError, match="registry"):
        resource("labwire:x", kind="wells", title="X", description="X.", content_model=Fine)
    # vendor-prefixed is fine
    resource("labwire:x", kind="acme.slot", title="X", description="X.", content_model=Fine)


def test_a_multi_segment_uri_is_not_declarable() -> None:
    class Fine(BaseModel):
        model_config = ConfigDict(extra="forbid")
        label: str

    with pytest.raises(TypeError, match="one path segment"):
        resource("labwire:deck/plate", kind="deck", title="X", description="X.", content_model=Fine)


# --- read -------------------------------------------------------------------


async def test_reading_returns_index_content_and_revision(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    instrument, _server, client = hotel
    instrument.slots["S2"] = "BC-0042"
    snapshot = await client.read_resource("labwire:hotel")
    assert snapshot.uri == "labwire:hotel"
    assert snapshot.index_complete is True
    entry = snapshot.index[0]
    assert entry.children is not None
    assert entry.children.ids == ["S1", "S2", "S3"]
    assert snapshot.content["occupied"][0]["plate_barcode"] == "BC-0042"
    assert snapshot.content["occupied"][0]["uri"] == "labwire:hotel/S2"


async def test_an_unknown_resource_uri_is_refused_helpfully(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = hotel
    with pytest.raises(UnknownReferenceError) as caught:
        await client.read_resource("labwire:freezer")
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "unknown_resource"
    assert "labwire:hotel" in str(caught.value)  # names what exists


async def test_the_revision_changes_when_content_changes_and_only_then(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    instrument, _server, client = hotel
    first = (await client.read_resource("labwire:hotel")).revision
    second = (await client.read_resource("labwire:hotel")).revision
    assert first == second  # derived, so a no-op read cannot move it
    instrument.slots["S1"] = "BC-1"
    third = (await client.read_resource("labwire:hotel")).revision
    assert third != first


async def test_touch_emits_the_reserved_event(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    instrument, _server, client = hotel
    events: list[tuple[str, dict[str, object]]] = []
    async with client.events() as stream:
        instrument.slots["S3"] = "BC-9"
        instrument.hotel.touch()
        async with asyncio.timeout(5.0):
            async for event in stream:
                if event.name == "resource/changed":
                    events.append((event.name, event.data))
                    break
    assert events[0][1]["uri"] == "labwire:hotel"
    assert isinstance(events[0][1]["revision"], str)


async def test_a_touch_that_changed_nothing_emits_nothing(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    instrument, _server, client = hotel
    await client.read_resource("labwire:hotel")  # settle the last revision
    seen: list[str] = []
    async with client.events() as stream:
        instrument.hotel.touch()  # nothing changed
        instrument.slots["S1"] = "BC-2"
        instrument.hotel.touch()  # this one changed
        async with asyncio.timeout(5.0):
            async for event in stream:
                if event.name == "resource/changed":
                    seen.append(str(event.data["revision"]))
                    break
    assert len(seen) == 1


# --- typed references -------------------------------------------------------


async def test_a_valid_reference_resolves_and_the_command_runs(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    instrument, _server, client = hotel
    handle = await client.submit(
        "store", {"slot": "labwire:hotel/S1", "barcode": "BC-7"}, confirmation=GRANT
    )
    await handle.result(timeout=5.0)
    assert instrument.slots["S1"] == "BC-7"


async def test_an_unknown_item_is_refused_with_the_full_error_shape(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = hotel
    with pytest.raises(UnknownReferenceError) as caught:
        await client.submit(
            "store", {"slot": "labwire:hotel/S9", "barcode": "BC-1"}, confirmation=GRANT
        )
    details = caught.value.details
    assert details is not None
    assert details["pointer"] == "/slot"
    assert details["reference"] == "labwire:hotel/S9"
    assert details["expected_kind"] == "site"
    assert details["reason"] == "no_such_item"
    assert details["resolved_prefix"] == "labwire:hotel"
    assert details["read"] == {"method": "resource/read", "params": {"uri": "labwire:hotel"}}
    assert "labwire:hotel/S1" in details["did_you_mean"]


async def test_a_kind_mismatch_is_named_as_such(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    """Passing the resource itself where a site is wanted is a kind error."""
    _instrument, _server, client = hotel
    with pytest.raises(UnknownReferenceError) as caught:
        await client.submit(
            "store", {"slot": "labwire:hotel", "barcode": "BC-1"}, confirmation=GRANT
        )
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "kind_mismatch"
    assert caught.value.details["resolved_kinds"] == ["deck"]


async def test_a_malformed_reference_is_refused_before_anything_else(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = hotel
    with pytest.raises(UnknownReferenceError) as caught:
        await client.submit(
            "store", {"slot": "just-a-slot-name", "barcode": "BC-1"}, confirmation=GRANT
        )
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "malformed_uri"


async def test_reference_failure_precedes_confirmation(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    """SPEC 12.1: an agent is never asked to confirm a call that cannot run."""
    _instrument, _server, client = hotel
    with pytest.raises(UnknownReferenceError):
        await client.submit("store", {"slot": "labwire:hotel/S9", "barcode": "B"})


async def test_reference_failure_never_spends_a_grant_or_creates_a_pending(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    _instrument, server, client = hotel
    with pytest.raises(UnknownReferenceError):
        await client.submit("purge", {"slot": "labwire:hotel/S9"})
    store = server._grant_store  # pyright: ignore[reportPrivateUsage]
    assert store is not None
    assert store.pending(now=server.clock.now()) == []


# --- if_revision ------------------------------------------------------------


async def test_a_fresh_revision_passes_and_a_stale_one_is_refused(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    _instrument, _server, client = hotel
    snapshot = await client.read_resource("labwire:hotel")

    handle = await client.submit(
        "store",
        {"slot": "labwire:hotel/S1", "barcode": "BC-1"},
        confirmation=GRANT,
        if_revision={"labwire:hotel": snapshot.revision},
    )
    await handle.result(timeout=5.0)

    # the same (now stale) revision is refused, with the recovery read
    with pytest.raises(StaleRevisionError) as caught:
        await client.submit(
            "store",
            {"slot": "labwire:hotel/S2", "barcode": "BC-2"},
            confirmation=GRANT,
            if_revision={"labwire:hotel": snapshot.revision},
        )
    details = caught.value.details
    assert details is not None
    assert details["submitted_revision"] == snapshot.revision
    assert details["current_revision"] != snapshot.revision
    assert details["read"] == {"method": "resource/read", "params": {"uri": "labwire:hotel"}}


async def test_the_terminal_status_reports_the_new_revision(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    """The write-returns-the-new-revision pattern: no re-read between steps."""
    _instrument, _server, client = hotel
    before = (await client.read_resource("labwire:hotel")).revision
    handle = await client.submit(
        "store", {"slot": "labwire:hotel/S1", "barcode": "BC-1"}, confirmation=GRANT
    )
    await handle.result(timeout=5.0)
    status = await handle.status()
    assert status.resource_revisions is not None
    assert status.resource_revisions[0].uri == "labwire:hotel"
    assert status.resource_revisions[0].revision != before

    # and the reported revision passes an if_revision check immediately
    follow_up = await client.submit(
        "store",
        {"slot": "labwire:hotel/S2", "barcode": "BC-2"},
        confirmation=GRANT,
        if_revision={"labwire:hotel": status.resource_revisions[0].revision},
    )
    await follow_up.result(timeout=5.0)


async def test_stale_revision_precedes_authorization(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    """A stale plan never costs an operator approval (SPEC 10.5)."""
    instrument, server, client = hotel
    old = (await client.read_resource("labwire:hotel")).revision
    instrument.slots["S1"] = "BC-1"  # the deck moves
    with pytest.raises(StaleRevisionError):
        await client.submit(
            "purge",
            {"slot": "labwire:hotel/S1"},
            if_revision={"labwire:hotel": old},
        )
    store = server._grant_store  # pyright: ignore[reportPrivateUsage]
    assert store is not None
    assert store.pending(now=server.clock.now()) == []  # nothing was recorded


# --- closure ----------------------------------------------------------------


def test_an_instrument_whose_references_dangle_cannot_be_described() -> None:
    class Dangling(Instrument):
        identity = IdentityInfo(
            manufacturer="m", model="d", serial_number="s", firmware_version="1"
        )

        @command(safety_class="S1")
        async def fetch(self, ctx: CommandContext, slot: Slot) -> None:  # pyright: ignore[reportInvalidTypeForm, reportUnknownParameterType]
            """Fetch from a slot, but no resource enumerates slots here."""

    with pytest.raises(Exception, match="labwire:hotel"):
        Dangling().describe()


def test_s3_with_a_grant_store_is_required_to_start() -> None:
    """SPEC 6.1: hazardous commands with no way to authorize them refuse to start."""
    instrument = PlateHotel()
    with pytest.raises(TypeError, match="grant store"):
        InstrumentServer(instrument)  # no grant_store, no LABWIRE_GRANT_STORE


async def test_purge_needs_a_grant_end_to_end(
    hotel: tuple[PlateHotel, InstrumentServer, LabwireClient],
) -> None:
    instrument, server, client = hotel
    instrument.slots["S1"] = "BC-1"
    with pytest.raises(AuthorizationRequiredError) as refused:
        await client.submit("purge", {"slot": "labwire:hotel/S1"}, confirmation=GRANT)
    assert refused.value.details is not None
    store = server._grant_store  # pyright: ignore[reportPrivateUsage]
    assert store is not None
    from datetime import timedelta

    grant = store.approve(
        refused.value.details["request_id"],
        now=server.clock.now(),
        ttl=timedelta(minutes=15),
        max_uses=1,
    )
    handle = await client.submit(
        "purge", {"slot": "labwire:hotel/S1"}, authorization=grant.grant_id
    )
    await handle.result(timeout=5.0)
    assert "S1" not in instrument.slots
