"""Talk to a Synapse device without importing ``science-synapse`` at module scope.

``science-synapse`` is an optional dependency. Everything it provides is
reached through :func:`protos`, which imports it on first use and raises a
plain :class:`ImportError` with an install line if it is absent, so
``labwire.bridges.synapse`` imports cleanly on a machine that does not have it.

Two facts about the pinned client shape this module, and both are recorded in
SYNAPSE.md rather than worked around silently:

1. **Every method of ``synapse.client.device.Device`` swallows
   ``grpc.RpcError``**, logs it, and returns ``None`` or ``False``. That
   includes the ``*_with_status`` variants, which are otherwise the only way
   to see a device's own error text. :class:`ClientErrorCapture` reads the
   detail back off the client's logger so an agent is told what happened
   instead of being handed a bare "no response".
2. **``Configure`` does not go through ``synapse.client.Config``.** That
   class holds ``nodes`` and ``connections`` as *class* attributes, so a
   freshly constructed ``Config()`` already contains every node any earlier
   ``Config`` was given (verified: ``a.nodes is b.nodes`` is True), and its
   ``ElectricalStimulation`` wrapper calls ``to_proto()`` on the protobuf
   ``Channel`` its own package exports, which has no such method. The bridge
   therefore builds ``DeviceConfiguration`` itself and sends it through the
   generated stub, which has the further merit of raising ``grpc.RpcError``
   instead of hiding it.

Example:
    >>> from labwire.bridges.synapse.client import SynapseTransport
    >>> # transport = SynapseTransport(synapse.Device("127.0.0.1:647"))
"""

import asyncio
import importlib
import logging
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from labwire.bridges.synapse.errors import OK, map_rpc_error, map_status, no_response
from labwire.core import LabwireError

CLIENT_LOGGER = "synapse.client.device"
"""The logger ``synapse.client.device.Device`` writes its swallowed errors to."""

_INSTALL_HINT = (
    "science-synapse is not installed; the Synapse bridge needs it. "
    'Install it with: pip install "science-synapse>=2.7,<3"'
)


@dataclass(frozen=True)
class Protos:
    """The ``science-synapse`` protobuf types this bridge builds and reads.

    Held as a frozen bundle so the import happens once and the rest of the
    package never writes ``import synapse`` at module scope.

    Example:
        >>> # protos().NodeType.kBroadbandSource
    """

    synapse: ModuleType
    grpc: ModuleType
    DeviceConfiguration: Any
    NodeConfig: Any
    NodeConnection: Any
    NodeType: Any
    Channel: Any
    SignalConfig: Any
    ElectrodeConfig: Any
    BroadbandSourceConfig: Any
    SpectralFilterConfig: Any
    SpectralFilterMethod: Any
    SpikeDetectorConfig: Any
    Thresholder: Any
    SpikeBinnerConfig: Any
    OpticalStimulationConfig: Any
    ElectricalStimulationConfig: Any
    QueryRequest: Any
    BroadbandFrame: Any
    Empty: Any


_protos_cache: Protos | None = None


def protos() -> Protos:
    """Import ``science-synapse`` on first use and return its types.

    Raises:
        ImportError: if ``science-synapse`` is not installed, with the
            install line in the message.

    Example:
        >>> # protos().QueryRequest.QueryType.kListTaps
    """
    global _protos_cache
    if _protos_cache is not None:
        return _protos_cache
    try:
        module = importlib.import_module
        synapse = module("synapse")
        grpc = module("grpc")
        device_pb2 = module("synapse.api.device_pb2")
        node_pb2 = module("synapse.api.node_pb2")
        channel_pb2 = module("synapse.api.channel_pb2")
        query_pb2 = module("synapse.api.query_pb2")
        datatype_pb2 = module("synapse.api.datatype_pb2")
        signal_pb2 = module("synapse.api.nodes.signal_config_pb2")
        broadband_pb2 = module("synapse.api.nodes.broadband_source_pb2")
        filter_pb2 = module("synapse.api.nodes.spectral_filter_pb2")
        detector_pb2 = module("synapse.api.nodes.spike_detector_pb2")
        binner_pb2 = module("synapse.api.nodes.spike_binner_pb2")
        optical_pb2 = module("synapse.api.nodes.optical_stimulation_pb2")
        electrical_pb2 = module("synapse.api.nodes.electrical_stimulation_pb2")
        empty_pb2 = module("google.protobuf.empty_pb2")
    except ImportError as exc:  # pragma: no cover - exercised by the absent-dep CI job
        raise ImportError(_INSTALL_HINT) from exc
    _protos_cache = Protos(
        synapse=synapse,
        grpc=grpc,
        DeviceConfiguration=device_pb2.DeviceConfiguration,
        NodeConfig=node_pb2.NodeConfig,
        NodeConnection=node_pb2.NodeConnection,
        NodeType=node_pb2.NodeType,
        Channel=channel_pb2.Channel,
        SignalConfig=signal_pb2.SignalConfig,
        ElectrodeConfig=signal_pb2.ElectrodeConfig,
        BroadbandSourceConfig=broadband_pb2.BroadbandSourceConfig,
        SpectralFilterConfig=filter_pb2.SpectralFilterConfig,
        SpectralFilterMethod=filter_pb2.SpectralFilterMethod,
        SpikeDetectorConfig=detector_pb2.SpikeDetectorConfig,
        Thresholder=detector_pb2.Thresholder,
        SpikeBinnerConfig=binner_pb2.SpikeBinnerConfig,
        OpticalStimulationConfig=optical_pb2.OpticalStimulationConfig,
        ElectricalStimulationConfig=electrical_pb2.ElectricalStimulationConfig,
        QueryRequest=query_pb2.QueryRequest,
        BroadbandFrame=datatype_pb2.BroadbandFrame,
        Empty=empty_pb2.Empty,
    )
    return _protos_cache


class ClientErrorCapture(logging.Handler):
    """Recover the gRPC detail the Synapse client logs instead of raising.

    ``Device.info()`` and friends catch ``grpc.RpcError``, call
    ``logger.error(...)`` on ``synapse.client.device``, and return ``None``.
    Without this handler the bridge could only report "no response"; with it
    the agent is told, for instance, that the connection was refused.

    Example:
        >>> capture = ClientErrorCapture()
        >>> capture.install()
        >>> capture.last is None
        True
        >>> capture.remove()
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.last: str | None = None
        self._logger = logging.getLogger(CLIENT_LOGGER)

    def emit(self, record: logging.LogRecord) -> None:
        """Record the message; never let logging raise into a device call."""
        try:
            self.last = record.getMessage()
        except Exception:
            self.last = "(the Synapse client logged a record this bridge could not format)"

    def install(self) -> None:
        """Attach to the Synapse client logger and make sure it emits errors."""
        self._logger.addHandler(self)
        if self._logger.level > logging.ERROR or self._logger.level == logging.NOTSET:
            self._logger.setLevel(logging.ERROR)

    def remove(self) -> None:
        """Detach from the Synapse client logger."""
        self._logger.removeHandler(self)

    def take(self) -> str | None:
        """Return the last captured message and forget it.

        Example:
            >>> ClientErrorCapture().take() is None
            True
        """
        message, self.last = self.last, None
        return message


class SynapseTransport:
    """One Synapse device, reached from asyncio with honest error reporting.

    The client is blocking, synchronous gRPC, so every call runs in a worker
    thread. A lock serializes them: the device has one signal chain and one
    lifecycle, and overlapping round trips would race the read-back that
    :meth:`configure` depends on.

    Example:
        >>> # transport = SynapseTransport(device)
        >>> # info = await transport.info()
    """

    def __init__(self, device: Any) -> None:
        self._device = device
        self._lock = asyncio.Lock()
        self._capture = ClientErrorCapture()
        self._capture.install()

    @property
    def device(self) -> Any:
        """The wrapped ``synapse.client.device.Device`` (or a stand-in)."""
        return self._device

    def close(self) -> None:
        """Stop capturing the Synapse client's logger.

        Example:
            >>> # transport.close()
        """
        self._capture.remove()

    async def _call(self, rpc: str, fn: Any) -> Any:
        async with self._lock:
            self._capture.take()
            try:
                result = await asyncio.to_thread(fn)
            except LabwireError:
                raise
            except Exception as exc:
                raise map_rpc_error(exc, rpc=rpc) from exc
            if result is None or result is False:
                raise no_response(rpc, self._capture.take())
            return result

    @staticmethod
    def _check(status: Any, rpc: str) -> None:
        if status is None:
            return  # a response that carries no status makes no claim to check
        code = int(getattr(status, "code", OK))
        if code != OK:
            raise map_status(code, str(getattr(status, "message", "")), rpc=rpc)

    async def info(self) -> Any:
        """Read ``DeviceInfo``: identity, state, peripherals, installed chain.

        Example:
            >>> # (await transport.info()).serial
        """
        info = await self._call("Info", self._device.info)
        self._check(getattr(info, "status", None), "Info")
        return info

    async def configure(self, configuration: Any) -> Any:
        """Replace the entire signal chain, through the generated stub.

        Synapse has no per-node configure: ``DeviceConfiguration`` is the
        whole chain, every time. The caller is responsible for having built
        the complete chain it wants.

        Example:
            >>> # await transport.configure(DeviceConfiguration(nodes=[...]))
        """
        status = await self._call("Configure", lambda: self._device.rpc.Configure(configuration))
        self._check(status, "Configure")
        return status

    async def start(self) -> Any:
        """Start the device (global; there is no per-node start in Synapse).

        Example:
            >>> # await transport.start()
        """
        status = await self._call("Start", self._device.start_with_status)
        self._check(status, "Start")
        return status

    async def stop(self) -> Any:
        """Stop the device (global; there is no per-node stop in Synapse).

        Example:
            >>> # await transport.stop()
        """
        status = await self._call("Stop", self._device.stop_with_status)
        self._check(status, "Stop")
        return status

    async def query(self, query_type: int) -> Any:
        """Run one ``Query`` and return the response, checking its status.

        Example:
            >>> # await transport.query(QueryRequest.QueryType.kListTaps)
        """
        request = protos().QueryRequest(query_type=query_type)
        response = await self._call("Query", lambda: self._device.query(request))
        self._check(getattr(response, "status", None), "Query")
        return response
