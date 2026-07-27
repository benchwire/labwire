"""Labwire driver for the SimPSU-3005 SCPI-style bench power supply.

Speaks generic SCPI conventions over TCP. Developed and tested against
:class:`labwire.sim.SimPowerSupply`; no real-hardware compatibility is
claimed.

Example:
    >>> from labwire.drivers import PowerSupply
    >>> # server = InstrumentServer(PowerSupply("127.0.0.1", 4003))
"""

import contextlib
from typing import TypedDict

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
from pydantic import ConfigDict

_POLL_S = 0.03
_SETTLE_TOLERANCE = 0.02
_SETTLE_TIMEOUT_S = 5.0


class VoltsResult(TypedDict):
    """The voltage setpoint actually reached."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]  # a closed result schema

    volts: float


class AmpsResult(TypedDict):
    """The current limit actually set."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]  # a closed result schema

    amps: float


class OutputResult(TypedDict):
    """Whether the output is now enabled."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]  # a closed result schema

    on: float


class MeasureResult(TypedDict):
    """One measurement of output voltage and current."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]  # a closed result schema

    volts: float
    amps: float


class LoadResult(TypedDict):
    """The simulated load resistance now applied."""

    __pydantic_config__ = ConfigDict(extra="forbid")  # pyright: ignore[reportGeneralTypeIssues]  # a closed result schema

    ohms: float


class PowerSupply(Instrument):
    """Driver for the Labwire SimPSU-3005 bench power supply.

    Example:
        >>> psu = PowerSupply("127.0.0.1", 4003)
    """

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="SimPSU-3005",
        serial_number="unconfigured",
        firmware_version="0.2.0",
    )

    voltage = channel(
        "voltage", unit="V", description="Measured output voltage.", qudt_quantity_kind="Voltage"
    )
    current = channel(
        "current",
        unit="A",
        description="Measured output current.",
        qudt_quantity_kind="ElectricCurrent",
    )
    over_current = interlock(
        "over_current",
        description="Over-current protection latched the output off. Cleared by clear_protection.",
        kind="soft",
    )

    def __init__(
        self, host: str = "", port: int = 0, *, link: LineProtocolClient | None = None
    ) -> None:
        super().__init__()
        # A prebuilt link (LineProtocolClient.serial(...) for USB-serial)
        # overrides host/port, which remain the TCP shorthand.
        self._link = link if link is not None else LineProtocolClient(host, port)

    async def on_start(self, server: InstrumentServer) -> None:
        """Connect, verify identification, and start the monitoring loop."""
        await self._link.open()
        idn = await self._link.command("*IDN?")
        if not idn.startswith("Labwire Project,SimPSU"):
            raise HardwareFaultError(f"unexpected device identification: {idn!r}")
        server.spawn(self._monitor_loop(server))

    async def on_stop(self) -> None:
        """Close the connection."""
        await self._link.close()

    async def _cmd(self, line: str) -> str:
        reply = await self._link.command(line)
        if reply.startswith("ERR"):
            raise HardwareFaultError(f"supply rejected {line.split()[0]!r}: {reply}")
        return reply

    async def _monitor_loop(self, server: InstrumentServer) -> None:
        while True:
            with contextlib.suppress(ConnectionError):
                status = dict(
                    field.split("=", 1) for field in (await self._cmd("STAT?")).split(",")
                )
                if status["OCP"] == "1":
                    self.over_current.trip()
                self.voltage.publish(float(await self._cmd("MEAS:VOLT?")))
                self.current.publish(float(await self._cmd("MEAS:CURR?")))
            await server.clock.sleep(_POLL_S)

    @command(
        units={"volts": "V"},
        returns_units={"volts": "V"},
        qudt_quantity_kind={"volts": "Voltage"},
        estimated_duration_s=2.0,
    )
    async def set_voltage(self, ctx: CommandContext, volts: float) -> VoltsResult:
        """Set the voltage setpoint and wait for the output to settle.

        Example:
            >>> # await client.submit("set_voltage", {"volts": 12.0})
        """
        await self._cmd(f"VOLT {volts}")
        if (await self._cmd("OUTP?")) != "1":
            return {"volts": volts}  # output off: setpoint stored, nothing to settle
        waited = 0.0
        while waited < _SETTLE_TIMEOUT_S:
            measured = float(await self._cmd("MEAS:VOLT?"))
            in_cc_mode = "MODE=CC" in await self._cmd("STAT?")
            if in_cc_mode or abs(measured - volts) <= _SETTLE_TOLERANCE * max(volts, 1.0):
                return {"volts": measured}
            if ctx.cancel_requested:
                return {"volts": measured}
            await ctx.sleep(_POLL_S)
            waited += _POLL_S
        raise DeviceTimeoutError(f"output did not settle to {volts} V")

    @command(units={"amps": "A"}, returns_units={"amps": "A"})
    async def set_current_limit(self, ctx: CommandContext, amps: float) -> AmpsResult:
        """Set the current limit (constant-current threshold).

        Example:
            >>> # await client.submit("set_current_limit", {"amps": 0.5})
        """
        await self._cmd(f"CURR {amps}")
        return {"amps": amps}

    @command(returns_units={"on": "1"})
    async def output(self, ctx: CommandContext, on: bool) -> OutputResult:
        """Enable or disable the output.

        Example:
            >>> # await client.submit("output", {"on": True})
        """
        await self._cmd(f"OUTP {'ON' if on else 'OFF'}")
        return {"on": float(int(on))}

    @command(returns_units={"volts": "V", "amps": "A"})
    async def measure(self, ctx: CommandContext) -> MeasureResult:
        """Measure output voltage and current.

        Example:
            >>> # await client.submit("measure", {})
        """
        volts = float(await self._cmd("MEAS:VOLT?"))
        amps = float(await self._cmd("MEAS:CURR?"))
        return {"volts": volts, "amps": amps}

    @command(clears_interlocks=["over_current"], safety_class="S0")
    async def clear_protection(self, ctx: CommandContext) -> dict[str, bool]:
        """Reset latched over-current protection.

        Example:
            >>> # await client.submit("clear_protection", {})
        """
        await self._cmd("CLPR")
        self.over_current.clear()
        return {"cleared": True}

    @command(name="x-sim/set_load", units={"ohms": "Ohm"}, returns_units={"ohms": "Ohm"})
    async def set_load(self, ctx: CommandContext, ohms: float) -> LoadResult:
        """Connect a resistive load (simulation-only vendor extension).

        Example:
            >>> # await client.submit("x-sim/set_load", {"ohms": 10.0})
        """
        await self._cmd(f"SIM:LOAD {ohms}")
        return {"ohms": ohms}

    @command(name="x-sim/inject_fault")
    async def inject_fault(self, ctx: CommandContext, kind: str) -> dict[str, str]:
        """Inject a simulated fault (``ocp``) or ``none`` to clear.

        Example:
            >>> # await client.submit("x-sim/inject_fault", {"kind": "ocp"})
        """
        await self._cmd(f"SIM:FAULT {kind}")
        return {"injected": kind}
