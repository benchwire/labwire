"""The signal chain the bridge holds, and how it becomes a Synapse proto.

Synapse has no per-node configuration RPC. ``Configure(DeviceConfiguration)``
replaces the entire chain, every time, so a bridge that exposed
``configure_broadband`` and ``configure_filter`` as independent commands would
have the second silently delete the first. This module is the answer: the
bridge keeps the chain it means to have, each ``configure_*`` command edits
one node of it, and every edit sends the whole chain and then reads it back.

The chain is bridge-side state and is honest about being so. It is committed
only after the device accepts it, it is republished in the ``labwire:device``
resource as ``pending_chain`` beside the chain the device actually reports,
and a configure that fails leaves the local chain untouched so the next
command is not poisoned by a node the device already rejected.

Node ordering is this bridge's convention, not a Synapse rule: the processing
nodes are connected in series in the order below, and stimulation nodes are
added unconnected because they are sinks fed by the device, not stages of the
acquisition path.

Example:
    >>> from labwire.bridges.synapse.chain import SignalChain, NodeSpec
    >>> chain = SignalChain().with_node(NodeSpec(kind="spectral_filter"))
    >>> chain.kinds()
    ['spectral_filter']
"""

import itertools
from dataclasses import dataclass, field, replace
from typing import Any

NODE_TYPES: dict[int, str] = {
    0: "kNodeTypeUnknown",
    3: "kBroadbandSource",
    4: "kElectricalStimulation",
    5: "kOpticalStimulation",
    6: "kSpikeDetector",
    7: "kSpikeSource",
    8: "kSpectralFilter",
    9: "kDiskWriter",
    10: "kSpikeBinner",
    11: "kApplication",
    12: "kCamera",
}
"""``synapse.NodeType``, verified against science-synapse 2.7.6."""

FILTER_METHODS: dict[str, int] = {
    "low_pass": 1,
    "high_pass": 2,
    "band_pass": 3,
    "band_stop": 4,
}
"""``synapse.SpectralFilterMethod`` under agent-readable names."""

PROCESSING_ORDER: tuple[str, ...] = (
    "broadband_source",
    "spectral_filter",
    "spike_detector",
    "spike_binner",
)
"""Acquisition stages, connected in series in this order."""

STIMULATION_KINDS: frozenset[str] = frozenset({"optical_stimulation", "electrical_stimulation"})
"""Node kinds that can drive tissue. Every command that installs one is S3."""

_PROTO_NODE_TYPE: dict[str, int] = {
    "broadband_source": 3,
    "electrical_stimulation": 4,
    "optical_stimulation": 5,
    "spike_detector": 6,
    "spectral_filter": 8,
    "spike_binner": 10,
}


def node_type_name(number: int) -> str:
    """Name a ``synapse.NodeType`` number, or say plainly that it is unknown.

    Example:
        >>> node_type_name(3), node_type_name(77)
        ('kBroadbandSource', 'unknown node type 77')
    """
    return NODE_TYPES.get(number, f"unknown node type {number}")


@dataclass(frozen=True)
class NodeSpec:
    """One node the bridge wants in the chain, in Labwire's own vocabulary.

    Quantities are held in the units the command declared (Hz, bit, uV, ms),
    and converted to the device's own field types only in :meth:`to_proto`,
    where the narrowing is visible.

    Example:
        >>> NodeSpec(kind="spike_binner", bin_size_ms=10.0).kind
        'spike_binner'
    """

    kind: str
    peripheral_id: int = 0
    sample_rate_hz: float | None = None
    bit_width: int | None = None
    gain: float | None = None
    low_cutoff_hz: float | None = None
    high_cutoff_hz: float | None = None
    electrode_ids: tuple[int, ...] = ()
    filter_method: str | None = None
    threshold_uV: float | None = None
    samples_per_spike: int | None = None
    bin_size_ms: float | None = None
    frame_rate_hz: float | None = None
    pixel_mask: tuple[int, ...] = ()

    def to_proto(self, node_id: int, protos: Any) -> Any:
        """Build the ``NodeConfig`` for this node.

        Raises:
            ValueError: if the node kind has no Synapse mapping. Reaching
                this is a bridge bug, not a device condition.

        Example:
            >>> # NodeSpec(kind="spectral_filter").to_proto(1, protos())
        """
        type_number = _PROTO_NODE_TYPE.get(self.kind)
        if type_number is None:
            raise ValueError(f"no Synapse node type for {self.kind!r}")
        config = protos.NodeConfig(type=type_number, id=node_id)
        builder = getattr(self, f"_build_{self.kind}")
        builder(config, protos)
        return config

    def _channels(self, protos: Any) -> list[Any]:
        return [
            protos.Channel(id=index, electrode_id=electrode, reference_id=electrode)
            for index, electrode in enumerate(self.electrode_ids)
        ]

    def _build_broadband_source(self, config: Any, protos: Any) -> None:
        config.broadband_source.CopyFrom(
            protos.BroadbandSourceConfig(
                peripheral_id=self.peripheral_id,
                bit_width=int(self.bit_width or 0),
                sample_rate_hz=int(self.sample_rate_hz or 0),
                gain=float(self.gain or 0.0),
                signal=protos.SignalConfig(
                    electrode=protos.ElectrodeConfig(
                        channels=self._channels(protos),
                        low_cutoff_hz=float(self.low_cutoff_hz or 0.0),
                        high_cutoff_hz=float(self.high_cutoff_hz or 0.0),
                    )
                ),
            )
        )

    def _build_spectral_filter(self, config: Any, protos: Any) -> None:
        config.spectral_filter.CopyFrom(
            protos.SpectralFilterConfig(
                method=FILTER_METHODS.get(self.filter_method or "", 0),
                low_cutoff_hz=float(self.low_cutoff_hz or 0.0),
                high_cutoff_hz=float(self.high_cutoff_hz or 0.0),
            )
        )

    def _build_spike_detector(self, config: Any, protos: Any) -> None:
        # Thresholder.threshold_uV is a uint32 on the wire, so a microvolt
        # threshold is truncated to a whole microvolt and cannot be negative.
        # The command validates that before it gets here.
        config.spike_detector.CopyFrom(
            protos.SpikeDetectorConfig(
                thresholder=protos.Thresholder(threshold_uV=int(self.threshold_uV or 0)),
                samples_per_spike=int(self.samples_per_spike or 0),
            )
        )

    def _build_spike_binner(self, config: Any, protos: Any) -> None:
        # bin_size_ms is a uint32 on the wire: whole milliseconds only.
        config.spike_binner.CopyFrom(
            protos.SpikeBinnerConfig(bin_size_ms=int(self.bin_size_ms or 0))
        )

    def _build_optical_stimulation(self, config: Any, protos: Any) -> None:
        config.optical_stimulation.CopyFrom(
            protos.OpticalStimulationConfig(
                peripheral_id=self.peripheral_id,
                pixel_mask=list(self.pixel_mask),
                bit_width=int(self.bit_width or 0),
                frame_rate=int(self.frame_rate_hz or 0),
                gain=float(self.gain or 0.0),
                send_receipts=False,
            )
        )

    def _build_electrical_stimulation(self, config: Any, protos: Any) -> None:
        config.electrical_stimulation.CopyFrom(
            protos.ElectricalStimulationConfig(
                peripheral_id=self.peripheral_id,
                channels=self._channels(protos),
                bit_width=int(self.bit_width or 0),
                sample_rate=int(self.sample_rate_hz or 0),
                lsb=int(self.gain or 1),
            )
        )


@dataclass(frozen=True)
class SignalChain:
    """The chain the bridge holds: at most one node of each kind.

    One node per kind is a bridge restriction, not a Synapse one. Synapse
    allows several nodes of a type; this bridge does not, because a command
    named ``configure_filter`` has no way to say *which* filter it means,
    and a positional index that an agent has to guess is exactly the kind of
    invented address SPEC §7.2 exists to remove.

    Example:
        >>> SignalChain().with_node(NodeSpec(kind="broadband_source")).kinds()
        ['broadband_source']
    """

    nodes: dict[str, NodeSpec] = field(default_factory=dict)

    def with_node(self, node: NodeSpec) -> "SignalChain":
        """Return a copy with ``node`` added or replacing its kind.

        Example:
            >>> SignalChain().with_node(NodeSpec(kind="spike_binner")).kinds()
            ['spike_binner']
        """
        return replace(self, nodes={**self.nodes, node.kind: node})

    def without(self, kind: str) -> "SignalChain":
        """Return a copy with the node of this kind removed, if present.

        Example:
            >>> SignalChain().without("spike_binner").kinds()
            []
        """
        return replace(self, nodes={k: v for k, v in self.nodes.items() if k != kind})

    def kinds(self) -> list[str]:
        """The chain's node kinds, in the order they would be sent.

        Example:
            >>> SignalChain().kinds()
            []
        """
        ordered = [kind for kind in PROCESSING_ORDER if kind in self.nodes]
        extras = sorted(kind for kind in self.nodes if kind not in PROCESSING_ORDER)
        return ordered + extras

    def stimulation_kinds(self) -> list[str]:
        """The stimulation nodes in the chain, if any.

        Example:
            >>> SignalChain().stimulation_kinds()
            []
        """
        return [kind for kind in self.kinds() if kind in STIMULATION_KINDS]

    def to_proto(self, protos: Any) -> Any:
        """Build the whole ``DeviceConfiguration``, ids and connections included.

        Node ids are assigned 1..n in :meth:`kinds` order. Consecutive
        processing nodes are connected in series; stimulation nodes get no
        connection.

        Example:
            >>> # SignalChain().to_proto(protos())
        """
        ordering = self.kinds()
        ids = {kind: index + 1 for index, kind in enumerate(ordering)}
        nodes = [self.nodes[kind].to_proto(ids[kind], protos) for kind in ordering]
        stages = [kind for kind in PROCESSING_ORDER if kind in self.nodes]
        connections = [
            protos.NodeConnection(src_node_id=ids[src], dst_node_id=ids[dst])
            for src, dst in itertools.pairwise(stages)
        ]
        return protos.DeviceConfiguration(nodes=nodes, connections=connections)
