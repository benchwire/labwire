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
from pylabrobot.resources import PLT_CAR_L5AC_A00, Cor_96_wellplate_360ul_Fb
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
    # Five empty plate sites, staging_0 through staging_4: the gripper's
    # legal destinations, indexed as kind "site".
    deck.assign_child_resource(PLT_CAR_L5AC_A00(name="staging"), rails=19)
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
        self.grant_dir: Path
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
        # The bridge declares S3 gripper commands, so a grant store is
        # mandatory: a server with S3 commands and no store refuses to start.
        # NOTE: this demo runs operator and agent as one user on one machine;
        # nothing here enforces the separation. On a real bench the store
        # lives where the agent cannot write it.
        rig.grant_dir = manifest_dir / "grants"
        server = InstrumentServer(
            instrument,
            confirmation_token=STANDING_GRANT,
            manifest_dir=manifest_dir,
            grant_store=rig.grant_dir,
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


async def gripper_act(rig: "DilutionRig") -> str:
    """The S3 ceremony, beat by beat, shared by both demos.

    Four things v0.2 could not make visible: the standing S2 grant that moved
    the whole dilution series does not move one plate; the refusal is
    productive (it creates the request a human approves); the approved grant
    moves the plate; and the same valid grant is refused on different
    parameters, which is the beat proving the binding is to parameters rather
    than an S3-shaped password.

    Returns the run id of the granted move, for signed-evidence verification.
    """
    from datetime import timedelta

    from labwire.core import AuthorizationRequiredError, GrantStore

    params = {"plate": "labwire:deck/dilution_plate", "to": "labwire:deck/staging-0"}

    print("\nmoving the dilution plate to the staging site (S3: gripper move)")
    try:
        await rig.call("move_plate", params)
        raise AssertionError("an S3 command ran on a confirmation; that is the F4 bug")
    except AuthorizationRequiredError as refused:
        details = refused.details or {}
        print(
            f"  REFUSED -32011  reason={details.get('reason')}  "
            f"mintable_by_agent={details.get('mintable_by_agent')}"
        )
        print(
            f"  the standing confirmation moved {8 * 100} uL of liquid this session; "
            "it does not move one plate"
        )
        request_id = str(details.get("request_id"))
        print(f"  operator instruction: {details.get('operator_instruction')}")

    # OPERATOR, separate role. This demo runs both as one user on one machine;
    # nothing here enforces the separation. On a real bench the grant store
    # lives where the agent cannot write it.
    print("\n  --- operator, on the instrument host ---")
    print("  $ labwire grant list")
    store = GrantStore(rig.grant_dir, serial_number="lh_deck")
    pending = store.find_pending(
        request_id, now=__import__("datetime").datetime.now(__import__("datetime").UTC)
    )
    assert pending is not None
    print(
        f"  {pending.request_id}   {pending.command}   S3   digest {pending.params_digest[:23]}..."
    )
    for name, value in sorted(pending.params.items()):
        print(f"    {name:6} {value}")
    print(f"  $ labwire grant approve {request_id} --ttl 15m --uses 1")
    grant = store.approve(
        request_id,
        now=__import__("datetime").datetime.now(__import__("datetime").UTC),
        ttl=timedelta(minutes=15),
        max_uses=1,
        issued_by="operator",
        note="plate to staging",
    )
    print(f"  grant {grant.grant_id[:10]}...  uses 0/1  expires {grant.expires_at}")
    print("  --- end operator ---\n")

    handle = await rig.client.submit("move_plate", params, authorization=grant.grant_id)
    moved = await handle.result(timeout=120.0)
    print(f"  GRANTED  {moved['moved']} -> {moved['to']}  (use 1/1)")

    # the beat that proves the binding: same grant, different plate
    try:
        await rig.client.submit(
            "move_plate",
            {"plate": "labwire:deck/source_plate", "to": "labwire:deck/staging-1"},
            authorization=grant.grant_id,
        )
        raise AssertionError("a spent, differently-bound grant was accepted")
    except AuthorizationRequiredError as mismatched:
        reason = (mismatched.details or {}).get("reason")
        print(
            f"  REFUSED -32011  reason={reason}  "
            "(a valid grant for the other plate does not move this one)"
        )
    return handle.command_id


def dilution_wells(steps: int) -> list[str]:
    """Addresses of the dilution series, across row A of the dilution plate.

    Example:
        >>> dilution_wells(2)
        ['labwire:deck/dilution_plate/A1', 'labwire:deck/dilution_plate/A2']
    """
    return [f"labwire:deck/dilution_plate/A{index + 1}" for index in range(steps)]


def demo_steps() -> int:
    """How many dilution steps to run (fewer under DEMO_FAST, for CI)."""
    return 3 if os.environ.get("DEMO_FAST") == "1" else 8
