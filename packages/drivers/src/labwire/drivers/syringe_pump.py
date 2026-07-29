"""Labwire driver for the SimPump-200 serial-style syringe pump.

Speaks the pump's native line protocol over TCP and exposes it as a Labwire
instrument. Developed and tested against :class:`labwire.sim.SimSyringePump`;
no real-hardware compatibility is claimed.

Example:
    >>> from labwire.drivers import SyringePump
    >>> # server = InstrumentServer(SyringePump("127.0.0.1", 4001))
"""

from typing import TypedDict

from labwire.core import (
    CanceledError,
    CommandContext,
    HardwareFaultError,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    InterlockError,
    ResourceSnapshot,
    channel,
    command,
    interlock,
    resource,
    unit_field,
)
from labwire.drivers._lineproto import LineProtocolClient
from pydantic import BaseModel, ConfigDict

_POLL_S = 0.02
_SETTLE_POLLS = 50  # settlement window: how long STP gets to prove itself


class SyringeInfo(BaseModel):
    """The installed syringe: the pump's one piece of tree-shaped state.

    Deliberately present on an instrument with **no references at all**: the
    resource primitive is not deck-shaped, and this exercises content typing,
    the derived revision, and change notification without a single
    resource_ref anywhere.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    capacity_ul: float = unit_field("uL")
    barrel_diameter_mm: float = unit_field("mm")
    installed_ul: float = unit_field("uL")
    """How much the syringe currently holds, by the pump's own accounting."""


class DispenseResult(TypedDict):
    """How much liquid was actually dispensed."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]  # a closed result schema

    dispensed_ul: float


class SyringePump(Instrument):
    """Driver for the Labwire SimPump-200 syringe pump.

    Example:
        >>> pump = SyringePump("127.0.0.1", 4001)
    """

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="SimPump-200",
        serial_number="unconfigured",
        firmware_version="0.2.0",
    )

    flow_rate = channel(
        "flow_rate",
        unit="uL/min",
        description="Commanded flow rate while running.",
        qudt_quantity_kind="VolumeFlowRate",
    )
    dispensed = channel(
        "dispensed",
        unit="uL",
        description="Cumulative dispensed volume this run.",
        qudt_quantity_kind="Volume",
    )
    occlusion = interlock(
        "occlusion",
        description="Line occlusion stalled the motor. Cleared by clear_occlusion.",
        kind="soft",
    )
    syringe = resource(
        "labwire:syringe",
        kind="consumable",
        title="Installed syringe",
        description=(
            "The syringe currently installed in the pump: its model, capacity, and "
            "how much it holds by the pump's own accounting. Changes when the "
            "plunger moves."
        ),
        content_model=SyringeInfo,
        item_kinds=[],
    )

    def __init__(
        self, host: str = "", port: int = 0, *, link: LineProtocolClient | None = None
    ) -> None:
        super().__init__()
        # A prebuilt link (LineProtocolClient.serial(...) for USB-serial)
        # overrides host/port, which remain the TCP shorthand.
        self._link = link if link is not None else LineProtocolClient(host, port)
        self._dispensed_total = 0.0

    @syringe.reader
    def _read_syringe(self) -> ResourceSnapshot:
        # The simulated pump models a 5 mL syringe; the capacity and barrel
        # figures describe that simulated hardware, not any vendor's.
        return ResourceSnapshot(
            index=[],
            content=SyringeInfo(
                model="SimSyringe-5000",
                capacity_ul=5000.0,
                barrel_diameter_mm=12.45,
                installed_ul=max(0.0, 5000.0 - self._dispensed_total),
            ),
        )

    async def on_start(self, server: InstrumentServer) -> None:
        """Open the pump connection and verify it answers.

        Example:
            >>> # called automatically by InstrumentServer.start()
        """
        await self._link.open()
        version = await self._link.command("VER?")
        if not version.startswith("LabwirePump,"):
            raise HardwareFaultError(f"unexpected device identification: {version!r}")

    async def on_stop(self) -> None:
        """Close the pump connection."""
        await self._link.close()

    async def _cmd(self, line: str) -> str:
        reply = await self._link.command(line)
        if reply.startswith("ERR"):
            raise HardwareFaultError(f"pump rejected {line.split()[0]!r}: {reply}")
        return reply

    def _parse_status(self, reply: str) -> tuple[str, float, float]:
        state, *fields = reply.split(",")
        values = dict(field.split("=", 1) for field in fields)
        return state, float(values["RATE"]), float(values["DISP"])

    @command(
        units={"volume_ul": "uL", "rate_ul_min": "uL/min"},
        returns_units={"dispensed_ul": "uL"},
        qudt_quantity_kind={"volume_ul": "Volume", "rate_ul_min": "VolumeFlowRate"},
        safety_class="S2",  # consumes reagent: irreversible (SPEC §8.6)
        cancel="abort",  # the pump protocol has a real stop (STP), confirmed below
        estimated_duration_s=60.0,
    )
    async def dispense(
        self, ctx: CommandContext, volume_ul: float, rate_ul_min: float
    ) -> DispenseResult:
        """Dispense a volume at a controlled flow rate, streaming progress.

        Example:
            >>> # await client.submit("dispense", {"volume_ul": 500.0, "rate_ul_min": 100.0})
        """
        await self._cmd(f"RAT {rate_ul_min}")
        await self._cmd(f"VOL {volume_ul}")
        await self._cmd("RUN")
        while True:
            state, rate, dispensed = self._parse_status(await self._cmd("STAT?"))
            self.flow_rate.publish(rate if state == "RUN" else 0.0)
            self.dispensed.publish(dispensed)
            if state == "STALL":
                self.occlusion.trip()
                raise InterlockError("occlusion detected: motor stalled")
            if state == "IDLE":
                await ctx.progress(1.0, "dispense complete")
                self._dispensed_total += dispensed
                self.syringe.touch()
                return {"dispensed_ul": dispensed}
            if ctx.cancel_requested:
                # SPEC 8.3: the pump has a real stop (STP), but sending it is
                # not the same as the motor stopping. Confirm before claiming.
                await self._cmd("STP")
                for _ in range(_SETTLE_POLLS):
                    state, _rate, dispensed = self._parse_status(await self._cmd("STAT?"))
                    if state == "IDLE":
                        self.flow_rate.publish(0.0)
                        self.dispensed.publish(dispensed)
                        self._dispensed_total += dispensed
                        self.syringe.touch()
                        ctx.confirm_halted(
                            f"pump reports IDLE after STP; {dispensed:.2f} uL had been dispensed"
                        )
                    await ctx.sleep(_POLL_S)
                raise CanceledError("STP sent; pump never reported IDLE within the window")
            await ctx.progress(min(dispensed / volume_ul, 1.0))
            await ctx.sleep(_POLL_S)

    @command(clears_interlocks=["occlusion"], safety_class="S0")
    async def clear_occlusion(self, ctx: CommandContext) -> dict[str, bool]:
        """Clear a stalled line after the occlusion has been resolved.

        Example:
            >>> # await client.submit("clear_occlusion", {})
        """
        await self._cmd("CLF")
        self.occlusion.clear()
        return {"cleared": True}

    @command(name="x-sim/inject_fault")
    async def inject_fault(self, ctx: CommandContext, kind: str) -> dict[str, str]:
        """Inject a simulated fault (``occlusion``) or ``none`` to clear.

        Simulation-only vendor extension (SPEC §7.5).

        Example:
            >>> # await client.submit("x-sim/inject_fault", {"kind": "occlusion"})
        """
        await self._cmd(f"SIM:FAULT {kind}")
        return {"injected": kind}
