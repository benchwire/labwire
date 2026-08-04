"""EXPERIMENTAL: expose a Science Corp Synapse device as a Labwire instrument.

``science-synapse`` is an optional dependency and is never imported at module
scope, so this package imports cleanly without it; you need it to have a
device to pass in. See SYNAPSE.md in this package for what was verified,
what was not, and every strain the mapping hit.

Verified against the ``synapse-sim`` simulator that ships with
``science-synapse`` and against no hardware of any kind. Not for clinical or
implanted use.

Example:
    >>> from labwire.bridges.synapse import unpack_version
    >>> unpack_version((2 << 20) | (4 << 10) | 1)
    '2.4.1'
"""

from labwire.bridges.synapse.bridge import (
    BROADBAND_MESSAGE_TYPE,
    MANUFACTURER,
    ChainResult,
    ImpedanceMeasurement,
    ImpedanceResult,
    InfoResult,
    RunResult,
    SelfTestItem,
    SelfTestResult,
    SettingsResult,
    SettingView,
    StopResult,
    SynapseBridge,
    SynapseInstrument,
    TapInfo,
    TapsResult,
    lsb_uv_from_info,
    unpack_version,
)
from labwire.bridges.synapse.chain import (
    FILTER_METHODS,
    NODE_TYPES,
    PROCESSING_ORDER,
    STIMULATION_KINDS,
    NodeSpec,
    SignalChain,
    node_type_name,
)
from labwire.bridges.synapse.client import (
    ClientErrorCapture,
    Protos,
    SynapseTransport,
    protos,
)
from labwire.bridges.synapse.errors import (
    DEVICE_STATES,
    STATUS_CODES,
    device_state_name,
    map_rpc_error,
    map_status,
    no_response,
    status_name,
)
from labwire.bridges.synapse.state import (
    DEVICE_URI,
    ConnectionView,
    DeviceState,
    NodeView,
    PeripheralView,
    PowerView,
    StorageView,
    TapView,
)

__all__ = [
    "BROADBAND_MESSAGE_TYPE",
    "DEVICE_STATES",
    "DEVICE_URI",
    "FILTER_METHODS",
    "MANUFACTURER",
    "NODE_TYPES",
    "PROCESSING_ORDER",
    "STATUS_CODES",
    "STIMULATION_KINDS",
    "ChainResult",
    "ClientErrorCapture",
    "ConnectionView",
    "DeviceState",
    "ImpedanceMeasurement",
    "ImpedanceResult",
    "InfoResult",
    "NodeSpec",
    "NodeView",
    "PeripheralView",
    "PowerView",
    "Protos",
    "RunResult",
    "SelfTestItem",
    "SelfTestResult",
    "SettingView",
    "SettingsResult",
    "SignalChain",
    "StopResult",
    "StorageView",
    "SynapseBridge",
    "SynapseInstrument",
    "SynapseTransport",
    "TapInfo",
    "TapView",
    "TapsResult",
    "device_state_name",
    "lsb_uv_from_info",
    "map_rpc_error",
    "map_status",
    "no_response",
    "node_type_name",
    "protos",
    "status_name",
    "unpack_version",
]
