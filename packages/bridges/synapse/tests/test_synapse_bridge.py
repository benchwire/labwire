"""The EXPERIMENTAL Synapse bridge, driven end to end against synapse-sim.

Every test here runs against the simulator that ships with ``science-synapse``,
through a real ``InstrumentServer`` and ``LabwireClient`` over
``MemoryTransport``. No hardware behaviour is claimed by any of them, and
several of them exist specifically to pin down what the simulator does *not*
implement, so the bridge's honesty about those gaps is itself under test.
"""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from labwire.bridges.synapse import (
    DEVICE_URI,
    SynapseInstrument,
    SynapseTransport,
    lsb_uv_from_info,
    map_status,
    node_type_name,
    unpack_version,
)
from labwire.core import (
    AuthorizationRequiredError,
    ConfirmationRequiredError,
    GrantStore,
    HardwareFaultError,
    InstrumentServer,
    InterlockError,
    LabwireClient,
    LabwireError,
    MemoryTransport,
    UnsupportedError,
    ValidationError,
)
from synapse_sim_support import CONFIRMATION, DEVICE_NAME, SERIAL

BROADBAND = {
    "sample_rate_hz": 30000.0,
    "bit_width": 12,
    "gain": 20.0,
    "electrode_ids": [0, 1, 2, 3],
    "low_cutoff_hz": 500.0,
    "high_cutoff_hz": 6000.0,
    "peripheral_id": 1,
}
OPTICAL = {
    "pixel_mask": [0, 1, 2, 3],
    "bit_width": 8,
    "frame_rate_hz": 30.0,
    "gain": 1.0,
    "peripheral_id": 1,
}


async def call(client: LabwireClient, name: str, params: dict[str, Any], **kwargs: Any) -> Any:
    """Submit and await one command, with the S2 confirmation supplied."""
    kwargs.setdefault("confirmation", CONFIRMATION)
    handle = await client.submit(name, params, **kwargs)
    return await handle.result(timeout=30.0)


# --- the descriptor ---------------------------------------------------------


async def test_descriptor_declares_units_classes_and_cancel_semantics(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """The whole point of the bridge, asserted in one place."""
    _, _, client = served
    descriptor = await client.describe()
    commands = {spec.name: spec for spec in descriptor.commands}

    assert set(commands) == {
        "get_info",
        "list_taps",
        "get_settings",
        "self_test",
        "measure_impedance",
        "configure_broadband",
        "configure_filter",
        "configure_spike_detect",
        "configure_spike_binner",
        "configure_optical_stimulation",
        "configure_electrical_stimulation",
        "clear_signal_chain",
        "start_acquisition",
        "start_stimulation",
        "apply_chain_and_start",
        "stop",
    }

    # Safety classes: the recovery paths are S0, impedance is S2 because it
    # injects current, and everything that can drive tissue is S3.
    assert commands["stop"].safety_class == "S0"
    assert commands["get_info"].safety_class == "S0"
    assert commands["clear_signal_chain"].safety_class == "S0"
    assert commands["measure_impedance"].safety_class == "S2"
    assert commands["configure_optical_stimulation"].safety_class == "S3"
    assert commands["configure_electrical_stimulation"].safety_class == "S3"
    assert commands["start_stimulation"].safety_class == "S3"
    assert commands["start_acquisition"].safety_class == "S1"

    # Units on every quantity Synapse leaves as a bare number.
    broadband = commands["configure_broadband"].unit_annotations
    assert broadband["sample_rate_hz"] == "Hz"
    assert broadband["bit_width"] == "bit"
    assert broadband["gain"] == "1"
    assert broadband["low_cutoff_hz"] == "Hz"
    assert broadband["electrode_ids"] == "1"
    assert commands["configure_spike_detect"].unit_annotations["threshold_uV"] == "uV"
    assert commands["configure_spike_binner"].unit_annotations["bin_size_ms"] == "ms"
    assert commands["measure_impedance"].returns_units["measurements[].magnitude"] == "Ohm"
    assert commands["measure_impedance"].returns_units["measurements[].phase"] == "deg"

    # Synapse has no abort RPC, so only the one command the bridge sequences
    # itself claims a boundary.
    boundaries = {name for name, spec in commands.items() if spec.cancel_semantics != "none"}
    assert boundaries == {"apply_chain_and_start"}
    assert commands["apply_chain_and_start"].cancel_semantics == "between_steps"

    # Identity never lets a manifest read as a hardware provenance claim.
    assert descriptor.identity.manufacturer == (
        "Science Corporation (via Synapse; never tested on hardware)"
    )
    assert descriptor.identity.model == DEVICE_NAME
    assert descriptor.identity.serial_number == SERIAL
    assert "synapse-api unreported" in descriptor.identity.firmware_version

    channels = {spec.name: spec for spec in descriptor.channels}
    assert channels["samples_received"].unit == "1"
    assert channels["frames_dropped"].unit == "1"
    assert channels["sample_rate_measured_hz"].unit == "Hz"
    assert channels["rms_counts"].unit == "1"
    # The simulator never reports lsb_uV, so the microvolt channel must not
    # exist: a channel that could never produce a sample is a false advert.
    assert "rms_uV" not in channels

    (device_resource,) = descriptor.resources
    assert device_resource.uri == DEVICE_URI
    assert device_resource.kind == "synapse.device"
    assert "synapse.node" in device_resource.item_kinds
    assert "synapse.tap" in device_resource.item_kinds

    assert descriptor.interlocks[0].name == "device_error"
    assert descriptor.interlocks[0].kind == "hard"


# --- the device resource ----------------------------------------------------


async def test_device_resource_is_honest_about_what_the_device_withheld(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """Nulls are named, never left to read as measurements."""
    _, _, client = served
    snapshot = await client.read_resource(DEVICE_URI)
    content = snapshot.content

    assert content["state"] == "kStopped"
    assert content["status_code"] == "kOk"
    assert content["serial"] == SERIAL
    assert content["nodes"] == []
    assert content["taps"] == []
    assert content["taps_read_at_state"] == "never"

    # The simulator reports none of these, and the projection says so by name
    # rather than publishing zeroes.
    assert content["synapse_api_version"] == "unreported"
    # Resource content is serialized with exclude_none, so an unreported
    # quantity is absent rather than null. That is exactly why the projection
    # also carries an explicit 'unavailable' list: absence alone is ambiguous.
    assert "lsb_uV" not in content
    assert content["microvolt_scale_available"] is False
    assert content["power"]["reported"] is False
    assert "battery_level_percent" not in content["power"]
    assert content["storage_reported"] is False
    unavailable = " | ".join(content["unavailable"])
    for missing in ("peripherals", "status.power", "status.storage", "lsb_uV", "synapse_version"):
        assert missing in unavailable


async def test_device_resource_indexes_the_installed_chain_and_taps(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """Configure then start, and the discovery surface follows."""
    _, _, client = served
    await call(client, "configure_broadband", BROADBAND)
    await call(
        client,
        "configure_filter",
        {"method": "band_pass", "low_cutoff_hz": 300.0, "high_cutoff_hz": 3000.0},
    )
    snapshot = await client.read_resource(DEVICE_URI)
    content = snapshot.content

    assert [node["type"] for node in content["nodes"]] == [
        "kBroadbandSource",
        "kSpectralFilter",
    ]
    source = content["nodes"][0]
    assert source["sample_rate_hz"] == 30000.0
    assert source["bit_width"] == 12
    assert source["gain"] == 20.0
    assert source["channel_count"] == 4
    assert source["electrode_ids"] == [0, 1, 2, 3]
    assert content["nodes"][1]["filter_method"] == "band_pass"
    assert content["connections"] == [{"src_node_id": 1, "dst_node_id": 2}]
    assert content["pending_chain"] == ["broadband_source", "spectral_filter"]

    node_uris = {entry.uri for entry in snapshot.index}
    assert {f"{DEVICE_URI}/node/1", f"{DEVICE_URI}/node/2"} <= node_uris

    result = await call(client, "start_acquisition", {})
    assert result["started"] is True
    assert result["state"] == "kRunning"
    assert result["tap_count"] == 1
    assert result["taps"][0]["message_type"] == "synapse.BroadbandFrame"
    assert result["telemetry_started"] is True
    assert "lsb_uV unreported" in result["telemetry_note"]

    after = await client.read_resource(DEVICE_URI)
    assert after.revision != snapshot.revision
    assert after.content["state"] == "kRunning"
    assert after.content["taps"][0]["tap_type"] == "TAP_TYPE_PRODUCER"
    assert any(entry.uri.startswith(f"{DEVICE_URI}/tap/") for entry in after.index)

    stopped = await call(client, "stop", {})
    assert stopped == {
        "stopped": True,
        "state": "kStopped",
        "was_running": True,
        "telemetry_stopped": True,
    }


# --- configure is proven, not trusted ---------------------------------------


async def test_a_rejected_configure_is_not_atomic_and_the_resource_says_so(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """A failed configure must not poison every later command.

    Measured, not assumed: the simulator implements four node types, and a
    Configure carrying an unsupported one installs every node up to the bad
    one and *then* answers with an error. So a rejected Configure leaves the
    device holding a prefix of what was sent. The bridge refreshes on the way
    out of the failure, so the resource shows the prefix rather than the chain
    that was asked for, and it does not commit its own chain, so the next
    command is not stuck behind a node the device already refused.
    """
    _, _, client = served
    await call(client, "configure_broadband", BROADBAND)
    with pytest.raises(LabwireError) as excinfo:
        await call(
            client, "configure_spike_detect", {"threshold_uV": 50.0, "samples_per_spike": 32}
        )
    assert "Failed to configure" in str(excinfo.value)

    content = (await client.read_resource(DEVICE_URI)).content
    assert [node["type"] for node in content["nodes"]] == ["kBroadbandSource"]
    assert content["pending_chain"] == ["broadband_source"]  # the detector was not committed

    # And the bridge is not stuck: the next configure succeeds.
    result = await call(client, "configure_broadband", BROADBAND)
    assert result["installed"] == ["kBroadbandSource"]


class _LyingDevice:
    """A device whose Configure claims success without doing anything.

    Not a hardware model: the point is the bridge's read-back guard. The
    simulator drops a node whose own configure fails while still answering
    kOk, so a bridge that trusted the status alone would report a stage that
    is not there.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self.rpc = _LyingStub(real)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _LyingStub:
    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real.rpc, name)

    def Configure(self, configuration: Any) -> Any:
        del configuration
        return self._real.info().status  # kOk, and nothing installed


async def test_a_configure_that_reports_ok_but_installs_nothing_is_caught(
    sim: str, grants: GrantStore
) -> None:
    """Configure returning kOk is not evidence that the chain is installed."""
    synapse = pytest.importorskip("synapse")
    instrument = SynapseInstrument(_LyingDevice(synapse.Device(sim)), telemetry_window_s=0.2)
    server = InstrumentServer(instrument, confirmation_token=CONFIRMATION, grant_store=grants)
    await server.start()
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        with pytest.raises(HardwareFaultError) as excinfo:
            await call(client, "configure_broadband", BROADBAND)
        assert "reported kOk but the device reports a different chain" in str(excinfo.value)
        assert excinfo.value.details is not None
        assert excinfo.value.details["sent"] == ["kBroadbandSource"]
        assert excinfo.value.details["installed"] == []
        # Nothing was committed, so the resource does not claim a chain either.
        assert (await client.read_resource(DEVICE_URI)).content["pending_chain"] == []
    await server.aclose()


async def test_clear_signal_chain_is_the_way_back(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """S0, so it stays available to remove whatever was installed."""
    _, _, client = served
    await call(client, "configure_broadband", BROADBAND)
    result = await client.submit("clear_signal_chain", {})  # S0: no confirmation
    cleared = await result.result(timeout=30.0)
    assert cleared["node_count"] == 0
    assert cleared["chain"] == []
    assert (await client.read_resource(DEVICE_URI)).content["nodes"] == []


async def test_fractional_values_are_refused_rather_than_truncated(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """A uint32 wire field must not quietly change a declared quantity."""
    _, _, client = served
    with pytest.raises(LabwireError, match="whole, non-negative number of milliseconds"):
        await call(client, "configure_spike_binner", {"bin_size_ms": 12.5})
    with pytest.raises(LabwireError, match="whole, non-negative number of microvolts"):
        await call(
            client, "configure_spike_detect", {"threshold_uV": 50.5, "samples_per_spike": 32}
        )


# --- error mapping ----------------------------------------------------------


async def test_a_query_against_a_stopped_device_maps_to_an_honest_error(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """The simulator refuses Query unless running; the bridge relays that."""
    _, _, client = served
    with pytest.raises(LabwireError) as excinfo:
        await call(client, "list_taps", {})
    assert "Device is not running" in str(excinfo.value)
    assert excinfo.value.details is not None
    assert excinfo.value.details["rpc"] == "Query"
    assert excinfo.value.details["synapse_status_code"] == "kUndefinedError"


async def test_stop_on_a_stopped_device_is_reported_not_sent(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """Stop is the recovery path, so it must not fail for being redundant.

    Synapse answers a Stop on a stopped device with an error, so the bridge
    reads the state first and says plainly that it sent nothing.
    """
    _, _, client = served
    handle = await client.submit("stop", {})
    result = await handle.result(timeout=30.0)
    assert result["was_running"] is False
    assert result["stopped"] is True
    assert result["state"] == "kStopped"


async def test_status_codes_map_across_the_taxonomy() -> None:
    """The table itself, without a device in the way."""
    assert type(map_status(2, "bad", rpc="Configure")).__name__ == "ValidationError"
    assert type(map_status(4, "no", rpc="Query")).__name__ == "UnsupportedError"
    assert type(map_status(5, "boom", rpc="Start")).__name__ == "InternalError"
    assert type(map_status(1, "", rpc="Stop")).__name__ == "HardwareFaultError"
    assert "the device sent no message" in str(map_status(1, "  ", rpc="Stop"))
    # A permission refusal from the device is NOT a Labwire authorization
    # decision, and must never be reported as one.
    assert type(map_status(6, "no", rpc="Start")).__name__ == "ValidationError"


async def test_an_unreachable_device_reports_the_detail_the_client_swallowed() -> None:
    """The client logs its gRPC errors and returns None; the bridge digs them out."""
    synapse = pytest.importorskip("synapse")
    transport = SynapseTransport(synapse.Device("127.0.0.1:1"))
    try:
        with pytest.raises(LabwireError) as excinfo:
            await transport.info()
    finally:
        transport.close()
    message = str(excinfo.value)
    assert "swallows grpc.RpcError" in message
    assert "Connection refused" in message or "failed to connect" in message


async def test_the_factory_refuses_a_device_that_will_not_answer() -> None:
    """No instrument is built on a device that never said hello."""
    synapse = pytest.importorskip("synapse")
    with pytest.raises(HardwareFaultError, match="did not answer Info"):
        SynapseInstrument(synapse.Device("127.0.0.1:1"))


# --- S2: impedance ----------------------------------------------------------


async def test_impedance_needs_a_confirmation_and_will_not_invent_a_reading(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """S2 gate first, then an honest refusal to fabricate a measurement.

    The simulator answers every query type with the tap list, so there is no
    impedance payload to report. The bridge says that rather than returning
    an empty sweep an agent could read as "all electrodes measured, none
    found".
    """
    _, _, client = served
    await call(client, "configure_broadband", BROADBAND)
    await call(client, "start_acquisition", {})

    with pytest.raises(ConfirmationRequiredError) as refused:
        await client.submit("measure_impedance", {})
    assert refused.value.details is not None
    assert refused.value.details["safety_class"] == "S2"

    with pytest.raises(UnsupportedError, match="will not invent one"):
        await call(client, "measure_impedance", {})


async def test_self_test_and_settings_refuse_the_same_way(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """Same simulator gap, same honest refusal."""
    _, _, client = served
    await call(client, "configure_broadband", BROADBAND)
    await call(client, "start_acquisition", {})
    with pytest.raises(UnsupportedError, match="kSelfTest"):
        await call(client, "self_test", {})
    with pytest.raises(UnsupportedError, match="kGetSettings"):
        await call(client, "get_settings", {})


# --- S3: stimulation --------------------------------------------------------


async def test_optical_stimulation_is_refused_without_an_operator_grant(
    served: tuple[Any, InstrumentServer, LabwireClient],
    grants: GrantStore,
) -> None:
    """The S3 path, which is the layer Labwire adds and Synapse has none of."""
    instrument, server, client = served

    with pytest.raises(AuthorizationRequiredError) as refused:
        await client.submit("configure_optical_stimulation", OPTICAL)
    details = refused.value.details
    assert details is not None
    assert details["safety_class"] == "S3"
    assert details["reason"] == "absent"
    assert details["mintable_by_agent"] is False

    # A confirmation must never satisfy an S3 command, whatever it contains.
    # Note the store replaces a pending request that repeats the same command
    # and digest, so the live request id is the one from the LAST refusal.
    with pytest.raises(AuthorizationRequiredError) as again:
        await client.submit("configure_optical_stimulation", OPTICAL, confirmation=CONFIRMATION)
    assert again.value.details is not None
    request_id = again.value.details["request_id"]

    grant = grants.approve(request_id, now=server.clock.now(), ttl=timedelta(minutes=5), max_uses=1)
    result = await call(
        client, "configure_optical_stimulation", OPTICAL, authorization=grant.grant_id
    )
    assert result["installed"] == ["kOpticalStimulation"]
    assert result["stimulation_nodes"] == ["kOpticalStimulation"]

    # The grant was bound to one use and is now spent.
    with pytest.raises(AuthorizationRequiredError) as spent:
        await client.submit("configure_optical_stimulation", OPTICAL, authorization=grant.grant_id)
    assert spent.value.details is not None
    assert spent.value.details["reason"] == "exhausted"
    del instrument


async def test_a_grant_does_not_carry_to_different_parameters(
    served: tuple[Any, InstrumentServer, LabwireClient],
    grants: GrantStore,
) -> None:
    """The grant binds the exact parameter digest, which is the whole point."""
    _, server, client = served
    with pytest.raises(AuthorizationRequiredError) as refused:
        await client.submit("configure_optical_stimulation", OPTICAL)
    assert refused.value.details is not None
    grant = grants.approve(
        refused.value.details["request_id"],
        now=server.clock.now(),
        ttl=timedelta(minutes=5),
        max_uses=2,
    )
    brighter = {**OPTICAL, "gain": 4.0}
    with pytest.raises(AuthorizationRequiredError) as mismatch:
        await client.submit("configure_optical_stimulation", brighter, authorization=grant.grant_id)
    assert mismatch.value.details is not None
    assert mismatch.value.details["reason"] == "params_mismatch"


async def test_an_s1_start_will_not_energize_a_configured_stimulator(
    served: tuple[Any, InstrumentServer, LabwireClient],
    grants: GrantStore,
) -> None:
    """Synapse's Start is device-global, so the S3 gate needs its own start.

    Without this refusal, an agent could pass the S3 gate on the configure
    and then energize the node with an S1 call.
    """
    _, server, client = served
    with pytest.raises(AuthorizationRequiredError) as refused:
        await client.submit("configure_optical_stimulation", OPTICAL)
    assert refused.value.details is not None
    grant = grants.approve(
        refused.value.details["request_id"],
        now=server.clock.now(),
        ttl=timedelta(minutes=5),
        max_uses=1,
    )
    await call(client, "configure_optical_stimulation", OPTICAL, authorization=grant.grant_id)

    with pytest.raises(ValidationError, match="use start_stimulation"):
        await call(client, "start_acquisition", {})
    with pytest.raises(ValidationError, match="use start_stimulation"):
        await call(client, "apply_chain_and_start", {})

    # start_stimulation is S3 and needs its own grant: the configure's grant
    # was bound to a different command.
    with pytest.raises(AuthorizationRequiredError) as start_refused:
        await client.submit("start_stimulation", {})
    assert start_refused.value.details is not None
    start_grant = grants.approve(
        start_refused.value.details["request_id"],
        now=server.clock.now(),
        ttl=timedelta(minutes=5),
        max_uses=1,
    )
    started = await call(client, "start_stimulation", {}, authorization=start_grant.grant_id)
    assert started["started"] is True
    assert started["state"] == "kRunning"


async def test_start_stimulation_refuses_an_ordinary_chain(
    served: tuple[Any, InstrumentServer, LabwireClient],
    grants: GrantStore,
) -> None:
    """It cannot be used as an unclassified way to start an acquisition."""
    _, server, client = served
    await call(client, "configure_broadband", BROADBAND)
    with pytest.raises(AuthorizationRequiredError) as refused:
        await client.submit("start_stimulation", {})
    assert refused.value.details is not None
    grant = grants.approve(
        refused.value.details["request_id"],
        now=server.clock.now(),
        ttl=timedelta(minutes=5),
        max_uses=1,
    )
    with pytest.raises(ValidationError, match="no stimulation node is installed"):
        await call(client, "start_stimulation", {}, authorization=grant.grant_id)


async def test_electrical_stimulation_is_gated_then_honestly_refused(
    served: tuple[Any, InstrumentServer, LabwireClient],
    grants: GrantStore,
) -> None:
    """Declared, grant-gated, and never verified against anything real.

    The shipped simulator does not implement kElectricalStimulation, so the
    device refuses the configure. The bridge relays that instead of pretending
    a stimulator was installed.
    """
    _, server, client = served
    params = {
        "electrode_ids": [0, 1],
        "bit_width": 12,
        "sample_rate_hz": 30000.0,
        "lsb": 1,
        "peripheral_id": 1,
    }
    with pytest.raises(AuthorizationRequiredError) as refused:
        await client.submit("configure_electrical_stimulation", params)
    assert refused.value.details is not None
    grant = grants.approve(
        refused.value.details["request_id"],
        now=server.clock.now(),
        ttl=timedelta(minutes=5),
        max_uses=1,
    )
    with pytest.raises(LabwireError, match="Failed to configure"):
        await call(client, "configure_electrical_stimulation", params, authorization=grant.grant_id)
    assert (await client.read_resource(DEVICE_URI)).content["nodes"] == []


# --- the interlock ----------------------------------------------------------


async def test_only_s0_stays_submittable_while_the_device_reports_an_error(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """The interlock is what makes the S0 assignments load-bearing."""
    instrument, _, client = served
    instrument.device_error.trip()

    with pytest.raises(InterlockError):
        await call(client, "configure_broadband", BROADBAND)

    # S0 commands stay available: the diagnostic, the stop, and the way back.
    info = await (await client.submit("get_info", {})).result(timeout=30.0)
    assert info["state"] in {"kStopped", "kRunning", "kInitializing"}
    stopped = await (await client.submit("stop", {})).result(timeout=30.0)
    assert stopped["stopped"] is True
    cleared = await (await client.submit("clear_signal_chain", {})).result(timeout=30.0)
    assert cleared["node_count"] == 0


# --- telemetry: the rate mismatch, reduced ----------------------------------


async def test_a_live_30khz_tap_is_reduced_to_one_sample_per_window(
    served: tuple[Any, InstrumentServer, LabwireClient],
) -> None:
    """The central strain, measured rather than asserted.

    The simulator publishes one ZeroMQ message per sample instant at the
    configured rate. What reaches the protocol is a handful of derived
    samples per second, each named for what it actually is.
    """
    _, _, client = served
    await call(client, "configure_broadband", BROADBAND)
    await call(client, "start_acquisition", {})

    seen: dict[str, float] = {}
    async with client.telemetry(
        ["samples_received", "frames_dropped", "sample_rate_measured_hz", "rms_counts"]
    ) as subscription:
        deadline = asyncio.get_event_loop().time() + 25.0
        while len(seen) < 4:
            assert asyncio.get_event_loop().time() < deadline, f"only got {sorted(seen)}"
            sample = await asyncio.wait_for(subscription.__anext__(), timeout=25.0)
            if sample.channel not in seen or sample.value:
                seen[sample.channel] = float(sample.value)

    # Frames really arrived, and at something like the configured rate. The
    # window is 0.2 s, so this is thousands of tap messages behind each
    # published sample.
    assert seen["samples_received"] > 100
    assert seen["sample_rate_measured_hz"] > 1000.0
    assert seen["rms_counts"] > 0.0
    assert seen["frames_dropped"] >= 0.0

    await call(client, "stop", {})


# --- cancellation, settled the way F10 demands ------------------------------


class _Stub:
    """A gRPC stub view that can hold ``Configure`` open on demand.

    Everything else is the real client against the real simulator; only the
    Configure round trip is stretched, which is the window a cancel has to
    land in for the boundary to mean anything.
    """

    def __init__(self, real: Any, on_configure: Any) -> None:
        self._real = real
        self.Configure = on_configure

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real.rpc, name)


class _HoldingDevice:
    """The real device with a stub whose Configure can be blocked."""

    def __init__(self, real: Any, on_configure: Any) -> None:
        self._real = real
        self.starts = 0
        self.rpc = _Stub(real, on_configure)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def start_with_status(self) -> Any:
        self.starts += 1
        return self._real.start_with_status()


async def test_a_cancel_during_apply_chain_and_start_settles_at_the_boundary(
    sim: str, grants: GrantStore
) -> None:
    """The one command that can honestly claim a boundary, proving it.

    A cancel accepted while the configure is in flight lets that configure
    finish and stops before Start is issued. The record names the boundary;
    the device is left configured and not started, which is exactly what the
    settlement says happened.
    """
    synapse = pytest.importorskip("synapse")
    real = synapse.Device(sim)
    gate = asyncio.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    seen = {"configures": 0}

    def blocking_configure(configuration: Any) -> Any:
        seen["configures"] += 1
        if seen["configures"] > 1:  # the first configure builds the chain freely
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(gate.wait(), loop).result(timeout=30)
        return real.rpc.Configure(configuration)

    holder = _HoldingDevice(real, blocking_configure)
    instrument = SynapseInstrument(holder, telemetry_window_s=0.2)
    server = InstrumentServer(instrument, confirmation_token=CONFIRMATION, grant_store=grants)
    await server.start()
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)
    async with LabwireClient.attach(client_end) as client:
        await call(client, "configure_broadband", BROADBAND)
        handle = await client.submit("apply_chain_and_start", {}, confirmation=CONFIRMATION)
        await asyncio.wait_for(entered.wait(), timeout=20.0)
        status = await handle.cancel()
        assert status.status == "canceling"
        gate.set()

        deadline = asyncio.get_event_loop().time() + 25.0
        while True:
            current = await handle.status()
            if current.status in ("succeeded", "failed", "canceled"):
                break
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(0.02)

        assert current.status == "canceled"
        assert current.cancellation is not None
        assert current.cancellation.outcome == "halted_at_boundary"
        assert current.cancellation.boundary is not None
        assert current.cancellation.boundary.last == "configure"
        assert current.cancellation.boundary.completed_steps == 1
        assert current.cancellation.boundary.of_steps == 2

        # Start was never issued, and the device says so.
        assert holder.starts == 0
        content = (await client.read_resource(DEVICE_URI)).content
        assert content["state"] == "kStopped"
        assert [node["type"] for node in content["nodes"]] == ["kBroadbandSource"]
    await server.aclose()


# --- the pure layers --------------------------------------------------------


def test_pure_helpers_need_no_device() -> None:
    """The layers that import nothing from science-synapse."""
    assert unpack_version((2 << 20) | (4 << 10) | 1) == "2.4.1"
    assert unpack_version(0) == "unreported"
    assert node_type_name(3) == "kBroadbandSource"
    assert node_type_name(999) == "unknown node type 999"
    assert lsb_uv_from_info(None) is None
