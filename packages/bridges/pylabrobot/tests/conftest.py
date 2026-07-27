"""A hardware-free liquid handler for the bridge's tests.

Everything here runs against ``LiquidHandlerChatterboxBackend``, which needs
no hardware, no server, and no browser: it prints the operations it would have
performed. PyLabRobot's simulator backend was removed in favour of a
websocket Visualizer that opens a browser, which is unusable in CI, so the
chatterbox backend is the only honest option.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
from pylabrobot.resources import (
    Cor_96_wellplate_360ul_Fb,
    set_tip_tracking,
    set_volume_tracking,
)
from pylabrobot.resources.hamilton import STARLetDeck, hamilton_96_tiprack_1000uL_filter

CHANNELS = 8


@pytest.fixture(autouse=True)
def _tracking() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Tracking on for every test, and restored afterwards.

    PyLabRobot toggles both trackers through module-level globals rather than
    per-handler state, so a test that left them on would change the behaviour
    of every later test in the process. See LIMITATIONS.
    """
    set_tip_tracking(True)
    set_volume_tracking(True)
    yield
    set_tip_tracking(False)
    set_volume_tracking(False)


@pytest.fixture
async def rig() -> AsyncIterator[LiquidHandler]:
    """A set-up liquid handler with a source plate, a target plate, and tips."""
    deck = STARLetDeck()
    handler = LiquidHandler(
        backend=LiquidHandlerChatterboxBackend(num_channels=CHANNELS), deck=deck
    )
    await handler.setup()
    deck.assign_child_resource(hamilton_96_tiprack_1000uL_filter(name="tips"), rails=1)
    deck.assign_child_resource(Cor_96_wellplate_360ul_Fb(name="source_plate"), rails=7)
    deck.assign_child_resource(Cor_96_wellplate_360ul_Fb(name="target_plate"), rails=13)
    yield handler
    await handler.stop()


@pytest.fixture
def bare_deck() -> object:
    """A deck with nothing assigned, for the empty and unassigned cases."""
    return STARLetDeck()
