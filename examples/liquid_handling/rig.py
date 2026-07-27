"""The dilution rig: a simulated liquid handler served over Labwire.

An ordinary PyLabRobot ``LiquidHandler`` on a Hamilton STARlet deck, with a
tip rack, a source plate, and a target plate, exposed through the Labwire
protocol by the bridge. The backend is PyLabRobot's chatterbox, so nothing
here needs hardware, a server, or a browser.

Everything an agent needs to plan comes back over the protocol: what labware
is loaded, how many tips are left, and which wells hold liquid.
"""

import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Self

from labwire.bridges.pylabrobot import PyLabRobotInstrument, load_annotations
from labwire.core import InstrumentServer, LabwireClient
from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
from pylabrobot.resources import Cor_96_wellplate_360ul_Fb
from pylabrobot.resources.hamilton import STARLetDeck, hamilton_96_tiprack_1000uL_filter

ANNOTATIONS = Path(__file__).parent / "labwire-pylabrobot.yaml"

# Every command that moves liquid is safety class S2 (SPEC 8.6): it consumes
# reagent and cannot be undone. A dilution series issues one per step, so the
# operator gives a standing grant for the session rather than confirming each
# transfer by hand. The grant is printed, so it is visible in the output.
STANDING_GRANT = "operator-standing-grant-dilution"

CHANNELS = 8
DYE_VOLUME_UL = 300.0
DILUENT_VOLUME_UL = 100.0
STEP_VOLUME_UL = 100.0


async def build_liquid_handler() -> LiquidHandler:
    """A configured liquid handler with tips and two plates, ready to serve.

    Separate from :class:`DilutionRig` so it can be pointed at directly::

        labwire-pylabrobot check examples.liquid_handling.rig:build_liquid_handler \\
            -a examples/liquid_handling/labwire-pylabrobot.yaml

    Example:
        >>> # handler = await build_liquid_handler()
    """
    deck = STARLetDeck()
    handler = LiquidHandler(
        backend=LiquidHandlerChatterboxBackend(num_channels=CHANNELS), deck=deck
    )
    await handler.setup()
    deck.assign_child_resource(hamilton_96_tiprack_1000uL_filter(name="tips"), rails=1)
    deck.assign_child_resource(Cor_96_wellplate_360ul_Fb(name="source_plate"), rails=7)
    deck.assign_child_resource(Cor_96_wellplate_360ul_Fb(name="dilution_plate"), rails=13)
    return handler


class DilutionRig:
    """Owns the liquid handler, its Labwire server, and the client.

    Example:
        >>> # async with await DilutionRig.start(Path("runs")) as rig:
        >>> #     await rig.describe_deck()
    """

    def __init__(self) -> None:
        self.client: LabwireClient
        self.manifest_dir: Path
        self._stack: AsyncExitStack

    @classmethod
    async def start(cls, manifest_dir: Path) -> Self:
        """Build the deck, serve it over WebSocket, and connect a client."""
        rig = cls()
        rig.manifest_dir = manifest_dir
        stack = AsyncExitStack()
        rig._stack = stack

        handler = await build_liquid_handler()
        instrument = PyLabRobotInstrument(handler, load_annotations(ANNOTATIONS))
        server = InstrumentServer(
            instrument, confirmation_token=STANDING_GRANT, manifest_dir=manifest_dir
        )
        stack.push_async_callback(server.aclose)
        ws_server = await stack.enter_async_context(server.serve_websocket("127.0.0.1", 0))
        port = ws_server.sockets[0].getsockname()[1]
        rig.client = await LabwireClient.connect(f"ws://127.0.0.1:{port}")
        await stack.enter_async_context(rig.client)
        return rig

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.aclose()

    async def call(self, name: str, params: dict[str, Any] | None = None) -> tuple[Any, str]:
        """Submit one command under the standing grant and wait for it.

        Returns the result and the run id, so the caller can find the signed
        bundle for any step it wants to verify.
        """
        handle = await self.client.submit(name, params or {}, confirmation=STANDING_GRANT)
        return await handle.result(timeout=120.0), handle.command_id

    def bundle_for(self, run_id: str) -> Path:
        """The signed bundle directory for one run."""
        return self.manifest_dir / run_id


def dilution_wells(steps: int) -> list[str]:
    """Addresses of the dilution series, across row A of the dilution plate.

    Example:
        >>> dilution_wells(3)
        ['dilution_plate/A1', 'dilution_plate/A2', 'dilution_plate/A3']
    """
    return [f"dilution_plate/A{index + 1}" for index in range(steps)]


def demo_steps() -> int:
    """How many dilution steps to run (fewer under DEMO_FAST, for CI)."""
    return 3 if os.environ.get("DEMO_FAST") == "1" else 8
