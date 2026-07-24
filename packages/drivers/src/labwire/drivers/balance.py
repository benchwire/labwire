"""Labwire driver for the SimBalance-120 analytical balance.

Streams the mass channel continuously in the background, exposes stable
measurement as a command, and maps the balance's overload marker to a
Labwire interlock. Developed and tested against
:class:`labwire.sim.SimBalance`; no real-hardware compatibility is claimed.

Example:
    >>> from labwire.drivers import Balance
    >>> # server = InstrumentServer(Balance("127.0.0.1", 4002))
"""

import contextlib

from labwire.core import (
    CommandContext,
    DeviceTimeoutError,
    HardwareFaultError,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    channel,
    command,
    interlock,
)
from labwire.drivers._lineproto import LineProtocolClient

_POLL_S = 0.03


class Balance(Instrument):
    """Driver for the Labwire SimBalance-120 analytical balance.

    Example:
        >>> balance = Balance("127.0.0.1", 4002)
    """

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="SimBalance-120",
        serial_number="unconfigured",
        firmware_version="0.1.0",
    )

    mass = channel("mass", unit="g", description="Continuously sampled net mass.")
    overload = interlock(
        "overload",
        description="Pan load exceeds capacity. Clears when the excess mass is removed.",
        kind="soft",
    )

    def __init__(self, host: str, port: int) -> None:
        super().__init__()
        self._link = LineProtocolClient(host, port)

    async def on_start(self, server: InstrumentServer) -> None:
        """Connect, verify identification, and start the sampling loop."""
        await self._link.open()
        version = await self._link.command("VER?")
        if not version.startswith("LabwireBalance,"):
            raise HardwareFaultError(f"unexpected device identification: {version!r}")
        server.spawn(self._sample_loop(server))

    async def on_stop(self) -> None:
        """Close the balance connection."""
        await self._link.close()

    async def _read(self) -> tuple[bool, float] | None:
        """One ``SI`` poll: (stable, net mass), or None while overloaded."""
        reply = await self._link.command("SI")
        if reply == "S +":
            self.overload.trip()
            return None
        parts = reply.split()  # "S", "S|D", "<val>", "g"
        if len(parts) != 4 or parts[0] != "S":
            raise HardwareFaultError(f"unparseable balance reply: {reply!r}")
        self.overload.clear()
        return parts[1] == "S", float(parts[2])

    async def _sample_loop(self, server: InstrumentServer) -> None:
        while True:
            with contextlib.suppress(ConnectionError):
                reading = await self._read()
                if reading is not None:
                    self.mass.publish(reading[1])
            await server.clock.sleep(_POLL_S)

    @command(units={"settle_timeout_s": "s"}, estimated_duration_s=5.0)
    async def measure(
        self, ctx: CommandContext, settle_timeout_s: float = 10.0
    ) -> dict[str, float]:
        """Wait for a stable reading and report it; emits ``measurement/stable``.

        Example:
            >>> # await client.submit("measure", {"settle_timeout_s": 10.0})
        """
        waited = 0.0
        while waited < settle_timeout_s:
            reading = await self._read()
            if reading is not None and reading[0]:
                ctx.emit_event("measurement/stable", "info", {"value": reading[1]})
                return {"mass_g": reading[1]}
            await ctx.sleep(_POLL_S)
            waited += _POLL_S
        raise DeviceTimeoutError(f"no stable reading within {settle_timeout_s} s")

    @command(units={"settle_timeout_s": "s"})
    async def tare(self, ctx: CommandContext, settle_timeout_s: float = 10.0) -> dict[str, float]:
        """Tare the balance, waiting for the pan to stabilize first.

        Example:
            >>> # await client.submit("tare", {})
        """
        waited = 0.0
        while waited < settle_timeout_s:
            reply = await self._link.command("T")
            if reply.startswith("T OK"):
                return {"tare_g": float(reply.split()[2])}
            if "unstable" not in reply:
                raise HardwareFaultError(f"tare rejected: {reply}")
            await ctx.sleep(_POLL_S)
            waited += _POLL_S
        raise DeviceTimeoutError(f"pan did not stabilize within {settle_timeout_s} s")

    @command(name="x-sim/load", units={"mass_g": "g"}, clears_interlocks=["overload"])
    async def load(self, ctx: CommandContext, mass_g: float) -> dict[str, float]:
        """Place a mass on the pan (simulation-only vendor extension).

        Declared with ``clears_interlocks`` because removing excess mass is
        how an overload is cleared.

        Example:
            >>> # await client.submit("x-sim/load", {"mass_g": 12.3456})
        """
        await self._link.command(f"SIM:LOAD {mass_g}")
        return {"loaded_g": mass_g}

    @command(name="x-sim/inject_fault")
    async def inject_fault(self, ctx: CommandContext, kind: str) -> dict[str, str]:
        """Inject a simulated fault (``vibration``) or ``none`` to clear.

        Example:
            >>> # await client.submit("x-sim/inject_fault", {"kind": "vibration"})
        """
        await self._link.command(f"SIM:FAULT {kind}")
        return {"injected": kind}
