"""The ``labwire:device`` resource: what a Synapse device says about itself.

Pure pydantic; nothing here imports ``science-synapse``. Every numeric field
carries a UCUM code through :func:`~labwire.core.unit_field`, because resource
content is state and state carries quantities (SPEC §7.6).

The models are deliberately generous about absence. A Synapse ``DeviceInfo``
is mostly optional in practice: the shipped simulator reports no peripherals,
no power, no storage, no signal-chain status and a packed API version of zero.
Rather than let those gaps read as zeroes, the projection nulls them and lists
each one by name in :attr:`DeviceState.unavailable`, so an agent reading the
resource can tell "the device did not say" from "the device said none".

Example:
    >>> from labwire.bridges.synapse.state import DeviceState
    >>> DeviceState(state="kStopped", status_code="kOk").state
    'kStopped'
"""

from typing import Annotated

from labwire.core import unit_field
from pydantic import BaseModel, ConfigDict, Field

Count = Annotated[int, Field(json_schema_extra={"unit": "1"})]
"""A dimensionless integer *inside a list*. The ``unit`` keyword has to sit on
the item node, not on the array node, because a code declared on the array
says nothing about what the array holds (SPEC §7.6)."""

DEVICE_URI = "labwire:device"
"""The one resource this bridge declares (SPEC §10.1)."""

NODE_KIND = "synapse.node"
TAP_KIND = "synapse.tap"
PERIPHERAL_KIND = "synapse.peripheral"
STORAGE_KIND = "synapse.storage_device"
"""Vendor-prefixed item kinds. None of the registry kinds (SPEC Appendix A)
describe a signal-chain node or a ZeroMQ tap, and inventing a bare name would
squat on the shared registry, so all four are ``<vendor>.<name>``."""


class _Model(BaseModel):
    """Closed base: resource content schemas must say what they contain."""

    model_config = ConfigDict(extra="forbid")


class PeripheralView(_Model):
    """One peripheral the device reports (``synapse.Peripheral``).

    Example:
        >>> PeripheralView(uri="labwire:device/peripheral/1", name="p",
        ...                vendor="v", peripheral_id=1, type="1").peripheral_id
        1
    """

    uri: str
    name: str
    vendor: str
    peripheral_id: int = unit_field("1")
    type: str
    address: str = ""


class NodeView(_Model):
    """One node of the installed signal chain, as the device reports it.

    The numeric members are the union of the node configs this bridge can
    build; each is null on a node type that has no such field. Flattening
    rather than nesting keeps one UCUM code per quantity visible at the top
    of the schema, where an agent reads it.

    Example:
        >>> NodeView(uri="labwire:device/node/1", node_id=1,
        ...          type="kBroadbandSource").node_id
        1
    """

    uri: str
    node_id: int = unit_field("1")
    type: str
    peripheral_id: int | None = unit_field("1", default=None)
    sample_rate_hz: float | None = unit_field("Hz", default=None)
    bit_width: int | None = unit_field("bit", default=None)
    gain: float | None = unit_field("1", default=None)
    low_cutoff_hz: float | None = unit_field("Hz", default=None)
    high_cutoff_hz: float | None = unit_field("Hz", default=None)
    channel_count: int | None = unit_field("1", default=None)
    electrode_ids: list[Count] = []
    filter_method: str | None = None
    threshold_uV: float | None = unit_field("uV", default=None)
    samples_per_spike: int | None = unit_field("1", default=None)
    bin_size_ms: float | None = unit_field("ms", default=None)
    frame_rate_hz: float | None = unit_field("Hz", default=None)
    pixel_mask: list[Count] = []
    lsb_uV: float | None = unit_field("uV", default=None)
    """Only ever set from ``NodeStatus.broadband_source.status.electrode``.
    Null means the device did not report a scale, not that the scale is zero."""


class ConnectionView(_Model):
    """One directed edge of the installed signal chain.

    Example:
        >>> ConnectionView(src_node_id=1, dst_node_id=2).dst_node_id
        2
    """

    src_node_id: int = unit_field("1")
    dst_node_id: int = unit_field("1")


class TapView(_Model):
    """One ZeroMQ tap the device is publishing on.

    The endpoint is reported verbatim. It is a plain ``tcp://`` address with
    no authentication and no transport security; see SYNAPSE.md, strain 4.

    Example:
        >>> TapView(uri="labwire:device/tap/t", name="t",
        ...         endpoint="tcp://127.0.0.1:5555", message_type="m",
        ...         tap_type="TAP_TYPE_PRODUCER").name
        't'
    """

    uri: str
    name: str
    endpoint: str
    message_type: str
    tap_type: str


class StorageView(_Model):
    """One storage device, if the device reports storage at all.

    Example:
        >>> StorageView(uri="labwire:device/storage/1", name="d",
        ...             storage_device_id=1).total_gb is None
        True
    """

    uri: str
    name: str
    storage_device_id: int = unit_field("1")
    total_gb: float | None = unit_field("GBy", default=None)
    used_gb: float | None = unit_field("GBy", default=None)


class PowerView(_Model):
    """Battery state, if the device reports power at all.

    Example:
        >>> PowerView(reported=False).battery_level_percent is None
        True
    """

    reported: bool
    battery_level_percent: float | None = unit_field("%", default=None)
    is_charging: bool | None = None


class DeviceState(_Model):
    """Everything this bridge can honestly say about one Synapse device.

    This is the discovery surface: an agent that reads it learns the device's
    identity, its lifecycle state, the chain that is actually installed (read
    back from the device, not the chain the bridge asked for), where the taps
    are, and precisely which of those the device declined to report.

    Example:
        >>> DeviceState(state="kRunning", status_code="kOk").state
        'kRunning'
    """

    state: str
    status_code: str
    status_message: str = ""
    name: str = ""
    serial: str = ""
    synapse_api_version: str = "unreported"
    synapse_version_packed: int = unit_field("1", default=0)
    firmware_version: int = unit_field("1", default=0)
    peripherals: list[PeripheralView] = []
    nodes: list[NodeView] = []
    connections: list[ConnectionView] = []
    taps: list[TapView] = []
    taps_read_at_state: str = "never"
    """The device state the tap list was last read in. Synapse refuses
    ``Query`` unless the device is running, so a stopped device's tap list is
    the last one seen, not a current one."""
    power: PowerView = PowerView(reported=False)
    storage: list[StorageView] = []
    storage_reported: bool = False
    lsb_uV: float | None = unit_field("uV", default=None)
    """The ADC scale, from ``NodeStatus.broadband_source.status.electrode``.
    Null means the device did not report one, so counts cannot be converted
    to microvolts and this bridge refuses to invent a factor."""
    microvolt_scale_available: bool = False
    pending_chain: list[str] = []
    """Node types the bridge holds and would send on the next configure, in
    order. Equal to the installed chain unless a configure failed."""
    unavailable: list[str] = []
    """Everything the device did not report, named. Read this before reading
    a null as a measurement."""
