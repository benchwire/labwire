"""Labwire driver for the SimPump-200 serial-style syringe pump.

Speaks the pump's native line protocol over TCP and exposes it as a Labwire
instrument. Developed and tested against :class:`labwire.sim.SimSyringePump`;
no real-hardware compatibility is claimed.

Example:
    >>> from labwire.drivers import SyringePump
    >>> # server = InstrumentServer(SyringePump("127.0.0.1", 4001))
"""

from labwire.core import (
    CanceledError,
    CommandContext,
    HardwareFaultError,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    InterlockError,
    channel,
    command,
    interlock,
)
from labwire.drivers._lineproto import LineProtocolClient

_POLL_S = 0.02


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

    def __init__(self, host: str, port: int) -> None:
        super().__init__()
        self._link = LineProtocolClient(host, port)

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
        estimated_duration_s=60.0,
    )
    async def dispense(
        self, ctx: CommandContext, volume_ul: float, rate_ul_min: float
    ) -> dict[str, float]:
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
                return {"dispensed_ul": dispensed}
            if ctx.cancel_requested:
                await self._cmd("STP")
                raise CanceledError("dispense stopped by cancel")
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
