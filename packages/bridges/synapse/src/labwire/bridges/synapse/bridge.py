"""Serve one Science Corp Synapse device through the Labwire protocol.

**EXPERIMENTAL.** Verified against the ``synapse-sim`` simulator that ships
with ``science-synapse`` and against nothing else. No claim is made about any
neural interface hardware, and nothing here is intended for clinical or
implanted use. See SYNAPSE.md in this package.

What Labwire adds to Synapse, which is the point of the exercise: UCUM codes
on every quantity Synapse leaves as a bare number, an S0-S3 class on every
command where Synapse has no notion of risk at all, a confirmation gate on
impedance measurement and an operator-grant gate on stimulation, honest
``cancel_semantics`` where Synapse has no abort RPC to be honest about, and a
signed run manifest recording what was commanded with which parameters.

What Labwire does **not** add: safety. Synapse's stimulation node configs
carry no amplitude, charge, or duty limits, so there is nothing here for a
protocol to bound. The honest claim is narrower and still worth something:
stimulation through this bridge is gated on an operator grant bound to the
exact parameter values, expiring and use-limited, and it is recorded.

Example:
    >>> # instrument = SynapseInstrument(synapse.Device("127.0.0.1:647"))
    >>> # server = InstrumentServer(instrument, grant_store=Path("grants"))
"""

import asyncio
import contextlib
import math
from typing import Any, Literal

from labwire.bridges.synapse.chain import (
    FILTER_METHODS,
    NodeSpec,
    SignalChain,
    node_type_name,
)
from labwire.bridges.synapse.client import SynapseTransport, protos
from labwire.bridges.synapse.errors import device_state_name, status_name
from labwire.bridges.synapse.state import (
    DEVICE_URI,
    NODE_KIND,
    PERIPHERAL_KIND,
    STORAGE_KIND,
    TAP_KIND,
    ConnectionView,
    DeviceState,
    NodeView,
    PeripheralView,
    PowerView,
    StorageView,
    TapView,
)
from labwire.core import (
    CommandContext,
    HardwareFaultError,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    LabwireError,
    ResourceIndexEntry,
    ResourceSnapshot,
    UnsupportedError,
    ValidationError,
    channel,
    command,
    interlock,
    resource,
)
from pydantic import BaseModel, ConfigDict

MANUFACTURER = "Science Corporation (via Synapse; never tested on hardware)"
"""Baked into every manifest. The parenthetical is not decoration: a manifest
that named the manufacturer alone would read as a hardware provenance claim."""

BROADBAND_MESSAGE_TYPE = "synapse.BroadbandFrame"
"""The only tap payload this bridge knows how to reduce."""

DEFAULT_TELEMETRY_WINDOW_S = 1.0
"""One derived sample per channel per second, from up to 30,000 tap frames."""

_QUERY_LIST_TAPS = 4
_QUERY_IMPEDANCE = 1
_QUERY_SAMPLE = 2
_QUERY_SELF_TEST = 3
_QUERY_GET_SETTINGS = 5
"""``synapse.QueryRequest.QueryType``, verified against science-synapse 2.7.6."""


def unpack_version(packed: int) -> str:
    """Unpack ``DeviceInfo.synapse_version`` into ``major.minor.patch``.

    The device packs it as ``(major & 0x3FF) << 20 | (minor & 0x3FF) << 10 |
    (patch & 0x3FF)``. Zero means the device did not report a version at all
    (the shipped simulator does exactly this, because the version file is not
    in the wheel), and saying "unreported" is more use than saying "0.0.0".

    Example:
        >>> unpack_version((2 << 20) | (4 << 10) | 1), unpack_version(0)
        ('2.4.1', 'unreported')
    """
    if packed <= 0:
        return "unreported"
    return f"{(packed >> 20) & 0x3FF}.{(packed >> 10) & 0x3FF}.{packed & 0x3FF}"


def lsb_uv_from_info(info: Any) -> float | None:
    """Dig the ADC scale out of a ``DeviceInfo``, or return None.

    It lives at ``status.signal_chain.nodes[].broadband_source.status.
    electrode.lsb_uV``: four levels down a status message that the device
    populates only when it feels like it, and that the shipped simulator
    never populates at all. Without it, ADC counts cannot honestly be
    converted to microvolts.

    Example:
        >>> lsb_uv_from_info(None) is None
        True
    """
    chain = getattr(getattr(info, "status", None), "signal_chain", None)
    for node in getattr(chain, "nodes", None) or []:
        value = float(getattr(node.broadband_source.status.electrode, "lsb_uV", 0.0) or 0.0)
        if value > 0:
            return value
    return None


class _Result(BaseModel):
    """Closed base for command results: a result schema must say what it holds."""

    model_config = ConfigDict(extra="forbid")


class TapInfo(_Result):
    """One tap the device is publishing on.

    Example:
        >>> TapInfo(name="t", endpoint="tcp://h:1", message_type="m",
        ...         tap_type="TAP_TYPE_PRODUCER").name
        't'
    """

    name: str
    endpoint: str
    message_type: str
    tap_type: str


class InfoResult(_Result):
    """What the device says about itself right now.

    Example:
        >>> InfoResult(state="kStopped", status_code="kOk").state
        'kStopped'
    """

    state: str
    status_code: str
    status_message: str = ""
    name: str = ""
    serial: str = ""
    synapse_api_version: str = "unreported"
    synapse_version_packed: int = 0
    firmware_version: int = 0
    peripheral_count: int = 0
    node_count: int = 0
    installed_chain: list[str] = []
    pending_chain: list[str] = []
    lsb_uV: float | None = None
    microvolt_scale_available: bool = False
    unavailable: list[str] = []


class TapsResult(_Result):
    """The device's current tap catalogue.

    Example:
        >>> TapsResult(tap_count=0).tap_count
        0
    """

    tap_count: int
    taps: list[TapInfo] = []


class ImpedanceMeasurement(_Result):
    """One electrode's impedance, as the device reported it.

    Example:
        >>> ImpedanceMeasurement(electrode_id=1, magnitude=1e5, phase=-45.0).phase
        -45.0
    """

    electrode_id: int
    magnitude: float
    phase: float


class ImpedanceResult(_Result):
    """An impedance sweep over the configured electrodes.

    Example:
        >>> ImpedanceResult(count=0).count
        0
    """

    count: int
    measurements: list[ImpedanceMeasurement] = []


class SelfTestItem(_Result):
    """One self-test the device ran.

    Example:
        >>> SelfTestItem(test_name="t", passed=True).passed
        True
    """

    test_name: str
    passed: bool
    test_report: str = ""


class SelfTestResult(_Result):
    """The device's self-test report.

    Example:
        >>> SelfTestResult(all_passed=True, tests_total=0, tests_passed=0).all_passed
        True
    """

    all_passed: bool
    tests_total: int
    tests_passed: int
    tests: list[SelfTestItem] = []


class SettingView(_Result):
    """One device setting, rendered as text.

    Values arrive as ``google.protobuf.Value``, which is an open mapping and
    therefore cannot carry unit codes; rendering to text keeps the result
    schema closed and does not pretend the value is a typed quantity.

    Example:
        >>> SettingView(name="s", value="1").value
        '1'
    """

    name: str
    value: str
    description: str = ""


class SettingsResult(_Result):
    """The device's settings, as it reports them.

    Example:
        >>> SettingsResult(count=0).count
        0
    """

    count: int
    settings: list[SettingView] = []


class ChainResult(_Result):
    """The signal chain after a configure, as the device reports it back.

    Example:
        >>> ChainResult(state="kStopped", node_count=0).node_count
        0
    """

    state: str
    node_count: int
    chain: list[str] = []
    installed: list[str] = []
    stimulation_nodes: list[str] = []


class RunResult(_Result):
    """The device after a start, and where its data is coming out.

    Example:
        >>> RunResult(started=True, state="kRunning", tap_count=0).started
        True
    """

    started: bool
    state: str
    tap_count: int
    taps: list[TapInfo] = []
    telemetry_started: bool = False
    telemetry_note: str = ""


class StopResult(_Result):
    """What ``stop`` did, and what the device claims about itself afterwards.

    Example:
        >>> StopResult(stopped=True, state="kStopped", was_running=True).was_running
        True
    """

    stopped: bool
    state: str
    was_running: bool
    telemetry_stopped: bool = False


_UNIT_ANNOTATIONS = {
    "sample_rate_hz": "Hz",
    "bit_width": "bit",
    "gain": "1",
    "low_cutoff_hz": "Hz",
    "high_cutoff_hz": "Hz",
    "electrode_ids": "1",
    "peripheral_id": "1",
    "threshold_uV": "uV",
    "samples_per_spike": "1",
    "bin_size_ms": "ms",
    "frame_rate_hz": "Hz",
    "pixel_mask": "1",
    "lsb": "1",
}
"""Every parameter name this bridge uses, with its UCUM code. Kept in one
place so two commands can never disagree about what ``gain`` means."""


def _units(*names: str) -> dict[str, str]:
    return {name: _UNIT_ANNOTATIONS[name] for name in names}


_INFO_RETURNS = {
    "synapse_version_packed": "1",
    "firmware_version": "1",
    "peripheral_count": "1",
    "node_count": "1",
    "lsb_uV": "uV",
}
_CHAIN_RETURNS = {"node_count": "1"}


class SynapseBridge(Instrument):
    """A Synapse device exposed as a Labwire instrument.

    Build it with :func:`SynapseInstrument`, which reads ``Info()`` once to
    derive identity and to decide whether the microvolt telemetry channel can
    honestly exist.

    Example:
        >>> # instrument = SynapseInstrument(device)
    """

    max_concurrent_commands = 1
    """A Synapse device has one signal chain and one global lifecycle."""

    device = resource(
        DEVICE_URI,
        kind="synapse.device",
        title="Synapse device",
        description=(
            "What this Synapse device reports about itself: lifecycle state, identity, "
            "the signal chain that is actually installed (read back from the device, "
            "not the one the bridge asked for), the ZeroMQ taps its data comes out of, "
            "peripherals, power and storage. Numeric members that the device did not "
            "report are null and are named in 'unavailable', so a null is never "
            "readable as a measurement. Refreshed whenever a command touches the "
            "device; 'get_info' refreshes it on demand."
        ),
        content_model=DeviceState,
        item_kinds=[NODE_KIND, TAP_KIND, PERIPHERAL_KIND, STORAGE_KIND],
    )

    device_error = interlock(
        "device_error",
        description=(
            "The device reported DeviceState.kError. Only S0 commands (get_info, "
            "stop, clear_signal_chain) stay submittable until it clears."
        ),
        kind="hard",
    )

    samples_received = channel(
        "samples_received",
        unit="1",
        dtype="int64",
        description=(
            "BroadbandFrames this bridge received from the tap during the last "
            "window. Frames the device published but ZeroMQ dropped are not counted "
            "here; see frames_dropped."
        ),
    )
    frames_dropped = channel(
        "frames_dropped",
        unit="1",
        dtype="int64",
        description=(
            "Frames missing from the tap's own sequence_number during the last "
            "window. Non-zero means the reduction lost data, said out loud."
        ),
    )
    sample_rate_measured_hz = channel(
        "sample_rate_measured_hz",
        unit="Hz",
        description=(
            "Frame arrival rate measured over the last window. Below the configured "
            "sample rate exactly when frames are being dropped."
        ),
        qudt_quantity_kind="Frequency",
    )
    rms_counts = channel(
        "rms_counts",
        unit="1",
        description=(
            "RMS of the received frame_data over the last window, in raw ADC counts. "
            "Counts, not microvolts: converting needs lsb_uV, which the device "
            "reports over a different transport and often not at all."
        ),
    )

    def __init__(
        self,
        transport: SynapseTransport,
        info: Any,
        *,
        telemetry_window_s: float = DEFAULT_TELEMETRY_WINDOW_S,
    ) -> None:
        super().__init__()
        self._transport = transport
        self._info: Any = info
        self._chain = SignalChain()
        self._taps: list[TapInfo] = []
        self._taps_state = "never"
        self._window_s = telemetry_window_s
        self._reducer: Any = None
        self._telemetry_stop = asyncio.Event()
        self._sync_interlock()

    # --- lifecycle ---------------------------------------------------------

    async def on_start(self, server: InstrumentServer) -> None:
        """Spawn the telemetry publisher.

        Example:
            >>> # await instrument.on_start(server)
        """
        self._telemetry_stop.clear()
        server.spawn(self._publish_telemetry())

    async def on_stop(self) -> None:
        """Stop the telemetry publisher and release the device wrapper.

        Example:
            >>> # await instrument.on_stop()
        """
        self._telemetry_stop.set()
        self._stop_reducer()
        self._transport.close()

    # --- plumbing ----------------------------------------------------------

    @device.reader
    def _read_device(self) -> ResourceSnapshot:
        return self._project()

    def _sync_interlock(self) -> None:
        """Mirror ``DeviceState.kError`` onto the declared interlock."""
        state = int(getattr(getattr(self._info, "status", None), "state", 0))
        if device_state_name(state) == "kError":
            self.device_error.trip()
        else:
            self.device_error.clear()

    async def _refresh(self) -> Any:
        """Re-read ``Info()``, mirror the interlock, and touch the resource."""
        self._info = await self._transport.info()
        self._sync_interlock()
        self.device.touch()
        return self._info

    def _installed_kinds(self) -> list[str]:
        nodes = getattr(getattr(self._info, "configuration", None), "nodes", [])
        return [node_type_name(int(node.type)) for node in nodes]

    def _installed_stimulation(self) -> list[str]:
        return [
            name
            for name in self._installed_kinds()
            if name in {"kOpticalStimulation", "kElectricalStimulation"}
        ]

    def _lsb_uv(self) -> float | None:
        """The ADC scale the device last reported, or None.

        Example:
            >>> # instrument._lsb_uv()
        """
        return lsb_uv_from_info(self._info)

    async def _configure(self, node: NodeSpec) -> ChainResult:
        """Send the whole chain with ``node`` in it, then prove it landed.

        Synapse replaces the entire chain on every ``Configure``, so this
        sends every node the bridge holds, not just the one that changed. The
        local chain is committed only after the device reports a chain that
        matches: the shipped simulator drops a node whose own configure fails
        and still answers ``kOk``, so a bridge that trusted the status alone
        would tell an agent a stage exists that does not.
        """
        candidate = self._chain.with_node(node)
        await self._send_chain(candidate)
        installed = self._installed_kinds()
        expected = [_EXPECTED_TYPE[kind] for kind in candidate.kinds()]
        if installed != expected:
            raise HardwareFaultError(
                "Configure reported kOk but the device reports a different chain: "
                f"sent {expected}, device reports {installed}. The local chain was "
                "not committed; read labwire:device for what is actually installed",
                details={"sent": expected, "installed": installed},
            )
        self._chain = candidate
        return self._chain_result()

    async def _send_chain(self, chain: SignalChain) -> None:
        """Send one whole chain and re-read the device, success or failure.

        A rejected Configure is destructive: the simulator clears the
        installed chain and leaves the device in kInitializing before it
        answers with an error. Refreshing on the way out of the failure is
        what keeps the labwire:device resource from describing a chain the
        device threw away a moment ago.
        """
        try:
            await self._transport.configure(chain.to_proto(protos()))
        except LabwireError:
            await self._refresh()
            raise
        await self._refresh()

    def _chain_result(self) -> ChainResult:
        installed = self._installed_kinds()
        return ChainResult(
            state=self._state_name(),
            node_count=len(installed),
            chain=self._chain.kinds(),
            installed=installed,
            stimulation_nodes=self._installed_stimulation(),
        )

    def _state_name(self) -> str:
        return device_state_name(int(getattr(getattr(self._info, "status", None), "state", 0)))

    @staticmethod
    def _whole(value: float, name: str, unit: str) -> int:
        """Refuse a fractional value the device's wire type cannot carry.

        Several Synapse config fields are ``uint32`` where the quantity is
        plainly continuous. Truncating silently would make the unit code a
        lie about the value that was sent, so the bridge refuses instead and
        says which field forced it.
        """
        if value < 0 or not math.isfinite(value) or value != int(value):
            raise ValidationError(
                f"{name} is a uint32 field in the Synapse protocol, so it must be a "
                f"whole, non-negative number of {unit}; {value} cannot be sent "
                "without silently changing it"
            )
        return int(value)

    def _refuse_stimulation_start(self, command_name: str) -> None:
        present = self._installed_stimulation() or [
            f"pending:{kind}" for kind in self._chain.stimulation_kinds()
        ]
        if present:
            raise ValidationError(
                f"{command_name} is S1 and will not energize a stimulation node. The "
                f"chain contains {present}; use start_stimulation, which is S3 and "
                "requires an operator grant, or clear_signal_chain first"
            )

    async def _read_taps(self) -> list[TapInfo]:
        response = await self._transport.query(_QUERY_LIST_TAPS)
        taps = [
            TapInfo(
                name=tap.name,
                endpoint=tap.endpoint,
                message_type=tap.message_type,
                tap_type=_TAP_TYPES.get(int(tap.tap_type), str(int(tap.tap_type))),
            )
            for tap in response.list_taps_response.taps
        ]
        self._taps = taps
        self._taps_state = self._state_name()
        self.device.touch()
        return taps

    def _start_reducer(self, taps: list[TapInfo]) -> tuple[bool, str]:
        from labwire.bridges.synapse.telemetry import TapReducer

        broadband = [tap for tap in taps if tap.message_type == BROADBAND_MESSAGE_TYPE]
        if not broadband:
            return False, (
                "no tap publishes "
                f"{BROADBAND_MESSAGE_TYPE}, so there is nothing this bridge can reduce"
            )
        self._stop_reducer()
        self._reducer = TapReducer(broadband[0].endpoint)
        self._reducer.start()
        note = f"reducing {broadband[0].name} at {broadband[0].endpoint}"
        if self._lsb_uv() is None:
            note += "; lsb_uV unreported, so rms is published in ADC counts only"
        return True, note

    def _stop_reducer(self) -> bool:
        if self._reducer is None:
            return False
        self._reducer.stop()
        self._reducer = None
        return True

    async def _publish_telemetry(self) -> None:
        """Publish one derived sample per channel per window, forever.

        This is the whole data-reduction contract in one loop: the worker
        thread absorbs the 30 kHz stream, and exactly this many samples per
        second reach the protocol.
        """
        scale = self._lsb_uv()
        while not self._telemetry_stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._telemetry_stop.wait(), timeout=self._window_s)
            reducer = self._reducer
            if reducer is None:
                continue
            window = reducer.take()
            self.samples_received.publish(window.frames)
            self.frames_dropped.publish(window.dropped)
            self.sample_rate_measured_hz.publish(window.rate_hz)
            rms = window.rms_counts
            if rms is not None:
                self.rms_counts.publish(rms)
                microvolts = getattr(self, "rms_uV", None)
                if microvolts is not None and scale is not None:
                    microvolts.publish(rms * scale)

    # --- projection --------------------------------------------------------

    def _project(self) -> ResourceSnapshot:
        info = self._info
        status = getattr(info, "status", None)
        unavailable: list[str] = []

        peripherals = [
            PeripheralView(
                uri=f"{DEVICE_URI}/peripheral/{item.peripheral_id}",
                name=item.name,
                vendor=item.vendor,
                peripheral_id=int(item.peripheral_id),
                type=str(int(item.type)),
                address=item.address,
            )
            for item in getattr(info, "peripherals", []) or []
        ]
        if not peripherals:
            unavailable.append("peripherals (the device reported none)")

        nodes = [
            _node_view(node, node_type_name(int(node.type)))
            for node in getattr(getattr(info, "configuration", None), "nodes", []) or []
        ]
        connections = [
            ConnectionView(src_node_id=int(edge.src_node_id), dst_node_id=int(edge.dst_node_id))
            for edge in getattr(getattr(info, "configuration", None), "connections", []) or []
        ]

        chain_status = getattr(status, "signal_chain", None)
        if not (getattr(chain_status, "nodes", []) or []):
            unavailable.append(
                "status.signal_chain (no per-node status, so no lsb_uV and no node health)"
            )

        power_proto: Any = getattr(status, "power", None)
        battery = float(getattr(power_proto, "battery_level_percent", 0.0) or 0.0)
        charging = bool(getattr(power_proto, "is_charging", False))
        # Synapse has no presence bit on DevicePower, so "reported nothing" and
        # "reported an empty battery while not charging" are the same message.
        # This bridge reads that pair as unreported and says so by name.
        has_power = bool(battery) or charging
        power = (
            PowerView(reported=True, battery_level_percent=battery, is_charging=charging)
            if has_power
            else PowerView(reported=False)
        )
        if not has_power:
            unavailable.append("status.power (battery level and charging state)")

        storage_devices = getattr(getattr(status, "storage", None), "storage_devices", []) or []
        storage = [
            StorageView(
                uri=f"{DEVICE_URI}/storage/{item.storage_device_id}",
                name=item.name,
                storage_device_id=int(item.storage_device_id),
                total_gb=float(item.total_gb),
                used_gb=float(item.used_gb),
            )
            for item in storage_devices
        ]
        if not storage:
            unavailable.append("status.storage (no storage devices reported)")

        packed = int(getattr(info, "synapse_version", 0) or 0)
        if packed <= 0:
            unavailable.append("synapse_version (the device reported 0)")
        scale = self._lsb_uv()
        if scale is None:
            unavailable.append(
                "lsb_uV (ADC counts cannot be converted to microvolts; no rms_uV channel)"
            )

        content = DeviceState(
            state=self._state_name(),
            status_code=status_name(int(getattr(status, "code", 0))),
            status_message=str(getattr(status, "message", "") or ""),
            name=str(getattr(info, "name", "") or ""),
            serial=str(getattr(info, "serial", "") or ""),
            synapse_api_version=unpack_version(packed),
            synapse_version_packed=packed,
            firmware_version=int(getattr(info, "firmware_version", 0) or 0),
            peripherals=peripherals,
            nodes=nodes,
            connections=connections,
            taps=[
                TapView(
                    uri=f"{DEVICE_URI}/tap/{tap.name}",
                    name=tap.name,
                    endpoint=tap.endpoint,
                    message_type=tap.message_type,
                    tap_type=tap.tap_type,
                )
                for tap in self._taps
            ],
            taps_read_at_state=self._taps_state,
            power=power,
            storage=storage,
            storage_reported=bool(storage),
            lsb_uV=scale,
            microvolt_scale_available=scale is not None,
            pending_chain=self._chain.kinds(),
            unavailable=unavailable,
        )
        index = [
            *(
                ResourceIndexEntry(uri=node.uri, kinds=[NODE_KIND], title=node.type)
                for node in nodes
            ),
            *(
                ResourceIndexEntry(uri=tap.uri, kinds=[TAP_KIND], title=tap.name)
                for tap in content.taps
            ),
            *(
                ResourceIndexEntry(uri=item.uri, kinds=[PERIPHERAL_KIND], title=item.name)
                for item in peripherals
            ),
            *(
                ResourceIndexEntry(uri=item.uri, kinds=[STORAGE_KIND], title=item.name)
                for item in storage
            ),
        ]
        return ResourceSnapshot(index=index, content=content)

    # --- reads -------------------------------------------------------------

    @command(safety_class="S0", cancel="none", returns_units=_INFO_RETURNS)
    async def get_info(self, ctx: CommandContext) -> InfoResult:
        """Read the device's identity, lifecycle state and installed chain.

        S0: a pure read with no device-side effect, and the diagnostic an
        operator needs most when the device is in kError and every other
        command is refused. Nothing is written, so nothing needs authorizing.

        Refreshes the labwire:device resource as a side effect, which is how
        an agent gets a current projection without a blocking call inside a
        resource read.
        """
        del ctx
        await self._refresh()
        info = self._info
        snapshot = self._project()
        content: DeviceState = snapshot.content
        packed = int(getattr(info, "synapse_version", 0) or 0)
        return InfoResult(
            state=content.state,
            status_code=content.status_code,
            status_message=content.status_message,
            name=content.name,
            serial=content.serial,
            synapse_api_version=content.synapse_api_version,
            synapse_version_packed=packed,
            firmware_version=content.firmware_version,
            peripheral_count=len(content.peripherals),
            node_count=len(content.nodes),
            installed_chain=self._installed_kinds(),
            pending_chain=self._chain.kinds(),
            lsb_uV=content.lsb_uV,
            microvolt_scale_available=content.microvolt_scale_available,
            unavailable=content.unavailable,
        )

    @command(safety_class="S1", cancel="none", returns_units={"tap_count": "1"})
    async def list_taps(self, ctx: CommandContext) -> TapsResult:
        """List the ZeroMQ endpoints the device is publishing data on.

        S1: a read, but one Synapse refuses unless the device is running, so
        it is not the always-available diagnostic that get_info is.

        The endpoints carry no authentication and no transport security. What
        comes back is the device's own claim about where its data is.
        """
        del ctx
        taps = await self._read_taps()
        return TapsResult(tap_count=len(taps), taps=taps)

    @command(safety_class="S1", cancel="none", returns_units={"count": "1"})
    async def get_settings(self, ctx: CommandContext) -> SettingsResult:
        """Read the device's settings, if it implements the settings query.

        S1: a read. Values are rendered as text because they arrive as
        google.protobuf.Value, which is an open mapping and cannot carry unit
        codes; a typed number with no declarable unit would be worse than a
        string.
        """
        del ctx
        response = await self._transport.query(_QUERY_GET_SETTINGS)
        if not response.HasField("get_settings_response"):
            raise UnsupportedError(_no_payload("get_settings", "kGetSettings"))
        payload = response.get_settings_response
        descriptors = {item.name: item.description for item in payload.schema}
        settings = [
            SettingView(name=name, value=str(value), description=descriptors.get(name, ""))
            for name, value in payload.settings.values.items()
        ]
        return SettingsResult(count=len(settings), settings=settings)

    @command(
        safety_class="S1",
        cancel="none",
        returns_units={"tests_total": "1", "tests_passed": "1"},
    )
    async def self_test(self, ctx: CommandContext) -> SelfTestResult:
        """Ask the device to run its self-tests and report the results.

        S1: the device decides what a self-test does. Synapse says nothing
        about whether kSelfTest energizes anything, so this class is a
        judgement about the query, not a guarantee about the hardware. On a
        device whose self-test drives current, this is the wrong class and
        the deployment should say so.
        """
        del ctx
        response = await self._transport.query(_QUERY_SELF_TEST)
        if not response.HasField("self_test_response"):
            raise UnsupportedError(_no_payload("self_test", "kSelfTest"))
        payload = response.self_test_response
        tests = [
            SelfTestItem(
                test_name=item.test_name, passed=bool(item.passed), test_report=item.test_report
            )
            for item in payload.tests
        ]
        return SelfTestResult(
            all_passed=bool(payload.all_passed),
            tests_total=len(tests),
            tests_passed=sum(1 for item in tests if item.passed),
            tests=tests,
        )

    @command(
        safety_class="S2",
        cancel="none",
        returns_units={
            "count": "1",
            "measurements[].electrode_id": "1",
            "measurements[].magnitude": "Ohm",
            "measurements[].phase": "deg",
        },
    )
    async def measure_impedance(self, ctx: CommandContext) -> ImpedanceResult:
        """Measure electrode impedance, which injects a test current.

        S2: this is not a passive read. An impedance measurement drives a
        known current through the electrodes to see what comes back, so it
        puts charge into whatever the electrodes are in contact with. That is
        a physical action with a cost, and it requires a confirmation.

        Magnitude comes back in ohms and phase in degrees, which is what
        Synapse's ImpedanceMeasurement carries; neither field is labelled in
        the protobuf, so the codes here are this bridge's reading of it.
        """
        del ctx
        response = await self._transport.query(_QUERY_IMPEDANCE)
        if not response.HasField("impedance_response"):
            raise UnsupportedError(_no_payload("measure_impedance", "kImpedance"))
        measurements = [
            ImpedanceMeasurement(
                electrode_id=int(item.electrode_id),
                magnitude=float(item.magnitude),
                phase=float(item.phase),
            )
            for item in response.impedance_response.measurements
        ]
        return ImpedanceResult(count=len(measurements), measurements=measurements)

    # --- configuration -----------------------------------------------------

    @command(
        safety_class="S1",
        cancel="none",
        units=_units(
            "sample_rate_hz",
            "bit_width",
            "gain",
            "electrode_ids",
            "low_cutoff_hz",
            "high_cutoff_hz",
            "peripheral_id",
        ),
        returns_units=_CHAIN_RETURNS,
    )
    async def configure_broadband(
        self,
        ctx: CommandContext,
        sample_rate_hz: float,
        bit_width: int,
        gain: float,
        electrode_ids: list[int],
        low_cutoff_hz: float = 0.0,
        high_cutoff_hz: float = 0.0,
        peripheral_id: int = 0,
    ) -> ChainResult:
        """Install a broadband acquisition source in the signal chain.

        S1: acquisition is routine and reversible, and reading a signal does
        nothing to the tissue.

        Synapse replaces the entire chain on every configure, so this sends
        every node the bridge is holding, not only this one, and then reads
        the chain back to prove it landed.
        """
        del ctx
        if not electrode_ids:
            raise ValidationError("configure_broadband needs at least one electrode id")
        return await self._configure(
            NodeSpec(
                kind="broadband_source",
                peripheral_id=peripheral_id,
                sample_rate_hz=float(self._whole(sample_rate_hz, "sample_rate_hz", "Hz")),
                bit_width=bit_width,
                gain=gain,
                electrode_ids=tuple(electrode_ids),
                low_cutoff_hz=low_cutoff_hz,
                high_cutoff_hz=high_cutoff_hz,
            )
        )

    @command(
        safety_class="S1",
        cancel="none",
        units=_units("low_cutoff_hz", "high_cutoff_hz"),
        returns_units=_CHAIN_RETURNS,
    )
    async def configure_filter(
        self,
        ctx: CommandContext,
        method: Literal["low_pass", "high_pass", "band_pass", "band_stop"],
        low_cutoff_hz: float = 0.0,
        high_cutoff_hz: float = 0.0,
    ) -> ChainResult:
        """Install a spectral filter stage after the source.

        S1: signal processing, reversible, no physical effect.
        """
        del ctx
        if method not in FILTER_METHODS:  # pragma: no cover - the Literal already forbids it
            raise ValidationError(f"unknown filter method {method!r}")
        return await self._configure(
            NodeSpec(
                kind="spectral_filter",
                filter_method=method,
                low_cutoff_hz=low_cutoff_hz,
                high_cutoff_hz=high_cutoff_hz,
            )
        )

    @command(
        safety_class="S1",
        cancel="none",
        units=_units("threshold_uV", "samples_per_spike"),
        returns_units=_CHAIN_RETURNS,
    )
    async def configure_spike_detect(
        self, ctx: CommandContext, threshold_uV: float, samples_per_spike: int
    ) -> ChainResult:
        """Install a threshold spike detector after the filter stage.

        S1: signal processing, reversible, no physical effect.

        The threshold is declared in microvolts but Synapse carries it as a
        uint32, so a fractional threshold is refused rather than truncated.
        """
        del ctx
        whole = self._whole(threshold_uV, "threshold_uV", "microvolts")
        return await self._configure(
            NodeSpec(
                kind="spike_detector",
                threshold_uV=float(whole),
                samples_per_spike=samples_per_spike,
            )
        )

    @command(
        safety_class="S1",
        cancel="none",
        units=_units("bin_size_ms"),
        returns_units=_CHAIN_RETURNS,
    )
    async def configure_spike_binner(self, ctx: CommandContext, bin_size_ms: float) -> ChainResult:
        """Install a spike binner after the detector.

        S1: signal processing, reversible, no physical effect.

        The bin size is declared in milliseconds but Synapse carries it as a
        uint32, so a fractional bin is refused rather than truncated.
        """
        del ctx
        whole = self._whole(bin_size_ms, "bin_size_ms", "milliseconds")
        return await self._configure(NodeSpec(kind="spike_binner", bin_size_ms=float(whole)))

    @command(
        safety_class="S3",
        cancel="none",
        units=_units("pixel_mask", "bit_width", "frame_rate_hz", "gain", "peripheral_id"),
        returns_units=_CHAIN_RETURNS,
    )
    async def configure_optical_stimulation(
        self,
        ctx: CommandContext,
        pixel_mask: list[int],
        bit_width: int,
        frame_rate_hz: float,
        gain: float,
        peripheral_id: int = 0,
    ) -> ChainResult:
        """Install an optical stimulation node: light delivered into tissue.

        S3, and this is the command the whole bridge exists to demonstrate.
        Synapse has no safety classification, no confirmation, no
        authorization and no limits: OpticalStimulationConfig carries a pixel
        mask, a bit width, a frame rate and a gain, and nothing that bounds
        optical power, duty cycle, or exposure. There is no dose to bound in
        the protocol, so this bridge does not pretend to bound one.

        What the S3 class does add is real and narrow: this exact parameter
        set must be authorized by an operator grant that is bound to the
        parameter digest, expires, and has a use limit, and the grant's
        digest is recorded in the signed run manifest. A confirmation value
        cannot satisfy it. Installing the node does not energize it; see
        start_stimulation.
        """
        del ctx
        if not pixel_mask:
            raise ValidationError("configure_optical_stimulation needs a non-empty pixel mask")
        return await self._configure(
            NodeSpec(
                kind="optical_stimulation",
                peripheral_id=peripheral_id,
                pixel_mask=tuple(pixel_mask),
                bit_width=bit_width,
                frame_rate_hz=float(self._whole(frame_rate_hz, "frame_rate_hz", "Hz")),
                gain=gain,
            )
        )

    @command(
        safety_class="S3",
        cancel="none",
        units=_units("electrode_ids", "bit_width", "sample_rate_hz", "lsb", "peripheral_id"),
        returns_units=_CHAIN_RETURNS,
    )
    async def configure_electrical_stimulation(
        self,
        ctx: CommandContext,
        electrode_ids: list[int],
        bit_width: int,
        sample_rate_hz: float,
        lsb: int,
        peripheral_id: int = 0,
    ) -> ChainResult:
        """Install an electrical stimulation node: current delivered into tissue.

        S3. Declared, gated, and never verified against anything that
        implements it: the shipped simulator does not support this node type
        and refuses the whole configure, so the only behaviour proven here is
        that the grant gate holds and the refusal is honest.

        ElectricalStimulationConfig carries a peripheral id, channels, a bit
        width, a sample rate and an LSB. It carries no amplitude, no pulse
        width, no charge limit and no duty limit, so there is nothing in the
        protocol for a bridge to bound. Read that plainly: Labwire makes this
        gated, parameter-bound and recorded. It does not make it safe.

        Note that a rejected configure is destructive on the device: the
        simulator clears the installed chain and leaves the device in
        kInitializing. Read labwire:device afterwards.
        """
        del ctx
        if not electrode_ids:
            raise ValidationError("configure_electrical_stimulation needs at least one electrode")
        return await self._configure(
            NodeSpec(
                kind="electrical_stimulation",
                peripheral_id=peripheral_id,
                electrode_ids=tuple(electrode_ids),
                bit_width=bit_width,
                sample_rate_hz=float(self._whole(sample_rate_hz, "sample_rate_hz", "Hz")),
                gain=float(lsb),
            )
        )

    @command(safety_class="S0", cancel="none", returns_units=_CHAIN_RETURNS)
    async def clear_signal_chain(self, ctx: CommandContext) -> ChainResult:
        """Replace the signal chain with an empty one.

        S0: this is the path that removes a stimulation node from the device,
        so it has to stay submittable when everything else is refused. It
        cannot install anything, so there is nothing for an operator to
        authorize.
        """
        del ctx
        await self._send_chain(SignalChain())
        self._chain = SignalChain()
        return self._chain_result()

    # --- lifecycle commands ------------------------------------------------

    @command(safety_class="S1", cancel="none", returns_units={"tap_count": "1"})
    async def start_acquisition(self, ctx: CommandContext) -> RunResult:
        """Start the device for acquisition, refusing to energize a stimulator.

        S1: starting an acquisition chain is routine. But Synapse's Start is
        device-global, with no per-node start, so on a chain that contains a
        stimulation node this same call would energize it. A static safety
        class cannot cover a hazard that an earlier command installed in
        device state, so this command refuses that case outright and points
        at start_stimulation, which is S3. Without that refusal the S3 gate
        on configure_optical_stimulation would be bypassable by an S1 call.

        cancel_semantics is "none": Synapse has no abort RPC, and Start is
        one round trip that is committed the moment it is issued.
        """
        del ctx
        await self._refresh()
        self._refuse_stimulation_start("start_acquisition")
        return await self._start()

    @command(safety_class="S3", cancel="none", returns_units={"tap_count": "1"})
    async def start_stimulation(self, ctx: CommandContext) -> RunResult:
        """Start a device whose chain contains a stimulation node.

        S3: this is the call that actually energizes a stimulator, and it is
        separated from start_acquisition precisely so that the authorization
        gate sits on the act of energizing rather than only on the act of
        configuring. It needs its own operator grant; the grant for the
        configure does not carry over, because it was bound to a different
        command and a different parameter digest.

        Refuses when no stimulation node is installed, so it cannot be used
        as an unclassified way to start an ordinary acquisition.
        """
        del ctx
        await self._refresh()
        if not (self._installed_stimulation() or self._chain.stimulation_kinds()):
            raise ValidationError(
                "start_stimulation refuses: no stimulation node is installed. Use "
                "start_acquisition for an ordinary acquisition chain"
            )
        return await self._start()

    @command(
        safety_class="S1",
        cancel="between_steps",
        returns_units={"tap_count": "1"},
        estimated_duration_s=2.0,
    )
    async def apply_chain_and_start(self, ctx: CommandContext) -> RunResult:
        """Re-send the held signal chain, then start acquisition.

        S1, and refuses a chain containing a stimulation node exactly as
        start_acquisition does.

        cancel_semantics is "between_steps", and this is the only command in
        this bridge that can honestly claim it. The bridge sequences two
        Synapse round trips, Configure then Start, and there is a real
        checkpoint between them: a cancel accepted while the configure is in
        flight lets that configure finish and stops before Start is issued,
        leaving the chain installed and the device never started. Neither
        step is itself interruptible, so a cancel never stops one mid-flight;
        the settlement record names the boundary that was reached.
        """
        await self._refresh()
        self._refuse_stimulation_start("apply_chain_and_start")
        await self._send_chain(self._chain)
        ctx.boundary("configure", of=2)
        return await self._start()

    async def _start(self) -> RunResult:
        await self._transport.start()
        await self._refresh()
        taps = await self._read_taps()
        started, note = self._start_reducer(taps)
        return RunResult(
            started=True,
            state=self._state_name(),
            tap_count=len(taps),
            taps=taps,
            telemetry_started=started,
            telemetry_note=note,
        )

    @command(safety_class="S0", cancel="none")
    async def stop(self, ctx: CommandContext) -> StopResult:
        """Stop the device: acquisition, stimulation, everything.

        S0: this is the recovery path, so it stays submittable while the
        device_error interlock is tripped and needs no confirmation.

        Read the result honestly. Synapse's Stop returning kOk means the
        device accepted the request and now reports kStopped; it does not
        mean emission has ceased, because Synapse offers no confirmation of
        physical state and this bridge has never been run against hardware.
        The state field is the device's own claim, relayed, not verified.
        Stopping an already-stopped device is reported rather than sent,
        because the device answers that request with an error.
        """
        del ctx
        await self._refresh()
        was_running = self._state_name() == "kRunning"
        if was_running:
            await self._transport.stop()
            await self._refresh()
        telemetry_stopped = self._stop_reducer()
        return StopResult(
            stopped=True,
            state=self._state_name(),
            was_running=was_running,
            telemetry_stopped=telemetry_stopped,
        )


_EXPECTED_TYPE: dict[str, str] = {
    "broadband_source": "kBroadbandSource",
    "spectral_filter": "kSpectralFilter",
    "spike_detector": "kSpikeDetector",
    "spike_binner": "kSpikeBinner",
    "optical_stimulation": "kOpticalStimulation",
    "electrical_stimulation": "kElectricalStimulation",
}

_TAP_TYPES: dict[int, str] = {
    0: "TAP_TYPE_UNSPECIFIED",
    1: "TAP_TYPE_PRODUCER",
    2: "TAP_TYPE_CONSUMER",
}

_FILTER_NUMBER_TO_NAME = {number: name for name, number in FILTER_METHODS.items()}


def _no_payload(command_name: str, query_type: str) -> str:
    return (
        f"{command_name}: the device answered the {query_type} query without a "
        f"{query_type} payload, so there is no reading to report. This bridge will not "
        "invent one. The shipped simulator answers every query type with the tap list, "
        "which is exactly this case"
    )


def _node_view(node: Any, type_name: str) -> NodeView:
    """Project one NodeConfig onto the flat, unit-annotated NodeView."""
    view = NodeView(uri=f"{DEVICE_URI}/node/{int(node.id)}", node_id=int(node.id), type=type_name)
    if node.HasField("broadband_source"):
        config = node.broadband_source
        electrode = config.signal.electrode
        return view.model_copy(
            update={
                "peripheral_id": int(config.peripheral_id),
                "sample_rate_hz": float(config.sample_rate_hz),
                "bit_width": int(config.bit_width),
                "gain": float(config.gain),
                "low_cutoff_hz": float(electrode.low_cutoff_hz),
                "high_cutoff_hz": float(electrode.high_cutoff_hz),
                "channel_count": len(electrode.channels),
                "electrode_ids": [int(item.electrode_id) for item in electrode.channels],
            }
        )
    if node.HasField("spectral_filter"):
        config = node.spectral_filter
        return view.model_copy(
            update={
                "filter_method": _FILTER_NUMBER_TO_NAME.get(
                    int(config.method), f"unknown method {int(config.method)}"
                ),
                "low_cutoff_hz": float(config.low_cutoff_hz),
                "high_cutoff_hz": float(config.high_cutoff_hz),
            }
        )
    if node.HasField("spike_detector"):
        config = node.spike_detector
        return view.model_copy(
            update={
                "threshold_uV": float(config.thresholder.threshold_uV),
                "samples_per_spike": int(config.samples_per_spike),
            }
        )
    if node.HasField("spike_binner"):
        return view.model_copy(update={"bin_size_ms": float(node.spike_binner.bin_size_ms)})
    if node.HasField("optical_stimulation"):
        config = node.optical_stimulation
        return view.model_copy(
            update={
                "peripheral_id": int(config.peripheral_id),
                "bit_width": int(config.bit_width),
                "frame_rate_hz": float(config.frame_rate),
                "gain": float(config.gain),
                "pixel_mask": [int(item) for item in config.pixel_mask],
            }
        )
    if node.HasField("electrical_stimulation"):
        config = node.electrical_stimulation
        return view.model_copy(
            update={
                "peripheral_id": int(config.peripheral_id),
                "bit_width": int(config.bit_width),
                "sample_rate_hz": float(config.sample_rate),
                "channel_count": len(config.channels),
                "electrode_ids": [int(item.electrode_id) for item in config.channels],
            }
        )
    return view


def SynapseInstrument(
    device: Any,
    *,
    telemetry_window_s: float = DEFAULT_TELEMETRY_WINDOW_S,
) -> SynapseBridge:
    """Build a Labwire instrument backed by a live Synapse device.

    Reads ``Info()`` once, synchronously, to derive identity and to decide
    whether the microvolt telemetry channel can honestly be declared. The
    ``rms_uV`` channel exists only when the device reported an ``lsb_uV``
    scale at construction: a channel that could never produce a sample would
    be a false advertisement in the descriptor, and a microvolt figure
    computed from a scale nobody reported would be worse.

    Raises:
        HardwareFaultError: if the device does not answer ``Info()``.

    Example:
        >>> # instrument = SynapseInstrument(synapse.Device("127.0.0.1:647"))
    """
    transport = SynapseTransport(device)
    info = device.info()
    if info is None:
        transport.close()
        raise HardwareFaultError(
            "the Synapse device did not answer Info(); the client swallows the gRPC "
            "error, so check that the device is reachable at the URI given"
        )
    packed = int(getattr(info, "synapse_version", 0) or 0)
    version = unpack_version(packed)
    identity = IdentityInfo(
        manufacturer=MANUFACTURER,
        model=str(getattr(info, "name", "") or "unnamed Synapse device"),
        serial_number=str(getattr(info, "serial", "") or "unknown"),
        firmware_version=(
            f"fw {int(getattr(info, 'firmware_version', 0) or 0)}; synapse-api {version}"
        ),
    )
    namespace: dict[str, Any] = {
        "identity": identity,
        "__doc__": (
            f"{identity.model} exposed through the EXPERIMENTAL Labwire Synapse bridge. "
            "Verified against the shipped synapse-sim simulator only; never against any "
            "neural interface hardware. Research rigs, not clinical or implanted use."
        ),
    }
    scale = lsb_uv_from_info(info)
    if scale is not None:
        namespace["rms_uV"] = channel(
            "rms_uV",
            unit="uV",
            description=(
                "RMS of the received frame_data over the last window, in microvolts, "
                f"using the lsb_uV scale of {scale} the device reported at startup."
            ),
            qudt_quantity_kind="Voltage",
        )
    generated = type("Synapse_Device", (SynapseBridge,), namespace)
    return generated(transport, info, telemetry_window_s=telemetry_window_s)
