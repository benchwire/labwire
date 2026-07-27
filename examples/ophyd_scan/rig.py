"""The ophyd scan rig: two simulated ophyd devices served over Labwire.

A sample stage (``ophyd.sim.SynAxis``) and a point detector
(``ophyd.sim.SynGauss``) whose response peaks at a hidden stage position.
Both are ordinary ophyd devices: the kind a beamline already has, exposed
through the Labwire protocol by the bridge, using the annotation file beside
this module for the units and safety classes ophyd does not carry.
"""

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Self

from labwire.bridges.ophyd import OphydInstrument, load_annotations
from labwire.core import InstrumentServer, LabwireClient
from ophyd.sim import SynAxis, SynGauss

ANNOTATIONS = Path(__file__).parent / "labwire-ophyd.yaml"

# `move` is safety class S2 (SPEC §8.6): it displaces the sample, so every
# submission needs operator confirmation. A scan issues one per point, so the
# operator issues a standing grant for the session rather than confirming
# each step by hand.
STANDING_GRANT = "operator-standing-grant-scan"

SCAN_RANGE = (-4.0, 4.0)
HIDDEN_CENTRE = 1.3  # what the scan is meant to discover
MOVE_DELAY_S = 0.02


class ScanRig:
    """Owns the ophyd devices, their Labwire servers, and the clients.

    Example:
        >>> # async with await ScanRig.start(Path("runs")) as rig:
        >>> #     await rig.move_to(1.0)
    """

    def __init__(self) -> None:
        self.stage_client: LabwireClient
        self.detector_client: LabwireClient
        self.manifest_dir: Path
        self._stack: AsyncExitStack

    @classmethod
    async def start(cls, manifest_dir: Path) -> Self:
        """Boot both devices, serve them over WebSocket, and connect clients."""
        rig = cls()
        rig.manifest_dir = manifest_dir
        stack = AsyncExitStack()
        rig._stack = stack
        annotations = load_annotations(ANNOTATIONS)

        stage = SynAxis(name="sample_stage", delay=MOVE_DELAY_S)
        detector = SynGauss(
            name="point_detector",
            motor=stage,
            motor_field="sample_stage",
            center=HIDDEN_CENTRE,
            Imax=1000.0,
            sigma=0.8,
            noise="uniform",
            noise_multiplier=15.0,
        )

        clients: list[LabwireClient] = []
        for index, device in enumerate((stage, detector)):
            instrument = OphydInstrument(device, annotations)
            server = InstrumentServer(
                instrument,
                confirmation_token=STANDING_GRANT,
                # the detector's readings are the evidence worth signing
                manifest_dir=manifest_dir if index == 1 else None,
            )
            stack.push_async_callback(server.aclose)
            ws_server = await stack.enter_async_context(server.serve_websocket("127.0.0.1", 0))
            port = ws_server.sockets[0].getsockname()[1]
            client = await LabwireClient.connect(f"ws://127.0.0.1:{port}")
            await stack.enter_async_context(client)
            clients.append(client)
        rig.stage_client, rig.detector_client = clients
        return rig

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.aclose()

    async def move_to(self, position_mm: float) -> float:
        """Move the stage (S2: presents the standing grant) and report where it landed."""
        handle = await self.stage_client.submit(
            "move", {"value": position_mm}, confirmation=STANDING_GRANT
        )
        result: dict[str, Any] = await handle.result(timeout=60.0)
        return float(result["value"])

    async def acquire(self) -> tuple[float, str]:
        """Trigger the detector (S1: no confirmation) and return counts and run id."""
        handle = await self.detector_client.submit("trigger", {})
        result: dict[str, Any] = await handle.result(timeout=60.0)
        return float(result["point_detector"]), handle.command_id

    def bundle_for(self, run_id: str) -> Path:
        """The signed bundle directory for one acquisition."""
        return self.manifest_dir / run_id
