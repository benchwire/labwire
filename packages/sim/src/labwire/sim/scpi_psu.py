"""Simulated bench power supply speaking SCPI-style commands over TCP.

An original Labwire device model ("SimPSU-3005", 30 V / 5 A): not an
emulation of any real vendor's supply. The command set follows generic SCPI
conventions (every command gets a reply line, for lockstep framing):

    *IDN?              -> Labwire Project,SimPSU-3005,SN-S3005007,0.1.0
    *RST               -> OK
    VOLT <v> / VOLT?   -> OK / <setpoint>
    CURR <a> / CURR?   -> OK / <limit>
    OUTP ON|OFF/OUTP?  -> OK / 1|0
    MEAS:VOLT?         -> <measured volts>
    MEAS:CURR?         -> <measured amps>
    STAT?              -> OUTP=<0|1>,MODE=<CV|CC>,OCP=<0|1>
    CLPR               -> OK            (clear protection)
    SIM:LOAD <ohms>    -> OK            (simulation-only: connected load)
    SIM:FAULT <name>   -> OK            (simulation-only: ocp | none)

Realism: voltage slews at a finite rate, measurements carry noise, the
supply enters constant-current mode when the load exceeds the current
limit, and over-current protection latches the output off.

Example:
    >>> from labwire.sim import SimPowerSupply
    >>> # sim = SimPowerSupply(seed=7); await sim.start()
"""

import asyncio
import random
from datetime import datetime

from labwire.core import Clock, SystemClock

_SLEW_V_PER_S = 50.0
_MAX_VOLT = 30.0
_MAX_CURR = 5.0
_NOISE_FRACTION = 0.002


class SimPowerSupply:
    """A simulated SCPI-style bench power supply TCP server.

    Example:
        >>> sim = SimPowerSupply(seed=7)
        >>> # await sim.start(); ...; await sim.stop()
    """

    def __init__(self, *, seed: int = 0, clock: Clock | None = None) -> None:
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.rng = random.Random(seed)
        self.serial_number = f"SN-S{3005000 + seed:07d}"
        self.setpoint_v = 0.0
        self.limit_a = 1.0
        self.output = False
        self.load_ohms = 100.0
        self.ocp_tripped = False
        self.fault: str | None = None
        self._slew_from_v = 0.0
        self._slew_started: datetime = self.clock.now()
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """The ephemeral TCP port the simulator listens on."""
        assert self._server is not None, "call start() first"
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        """Start listening on 127.0.0.1 (ephemeral port)."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    async def stop(self) -> None:
        """Stop the TCP server."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # -- physics ---------------------------------------------------------------

    def _rail_v(self) -> float:
        """Setpoint-tracking rail voltage, slew-limited, before load effects."""
        elapsed = (self.clock.now() - self._slew_started).total_seconds()
        delta = self.setpoint_v - self._slew_from_v
        travel = min(abs(delta), _SLEW_V_PER_S * elapsed)
        return self._slew_from_v + (travel if delta >= 0 else -travel)

    def _tick_protection(self) -> None:
        if self.output and self.fault == "ocp":
            self.ocp_tripped = True
            self.output = False

    def _measure(self) -> tuple[float, float, str]:
        """(volts, amps, mode) at the output terminals."""
        self._tick_protection()
        if not self.output:
            return 0.0, 0.0, "CV"
        rail = self._rail_v()
        amps = rail / self.load_ohms
        mode = "CV"
        if amps > self.limit_a:  # constant-current mode
            amps = self.limit_a
            rail = amps * self.load_ohms
            mode = "CC"
        noise = 1.0 + self.rng.gauss(0.0, _NOISE_FRACTION)
        return rail * noise, amps * noise, mode

    def _set_voltage(self, volts: float) -> None:
        self._slew_from_v = self._rail_v()
        self._slew_started = self.clock.now()
        self.setpoint_v = volts

    # -- protocol --------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                await self.clock.sleep(self.rng.uniform(0.001, 0.005))  # command latency
                reply = self._handle(line.decode().strip())
                writer.write((reply + "\r\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    def _handle(self, line: str) -> str:
        cmd, _, arg = line.partition(" ")
        match cmd.upper():
            case "*IDN?":
                return f"Labwire Project,SimPSU-3005,{self.serial_number},0.1.0"
            case "*RST":
                self.setpoint_v = 0.0
                self.limit_a = 1.0
                self.output = False
                self.load_ohms = 100.0
                self.ocp_tripped = False
                self.fault = None
                self._slew_from_v = 0.0
                self._slew_started = self.clock.now()
                return "OK"
            case "VOLT?":
                return f"{self.setpoint_v:.4f}"
            case "VOLT":
                try:
                    volts = float(arg)
                except ValueError:
                    return "ERR bad voltage"
                if not 0.0 <= volts <= _MAX_VOLT:
                    return "ERR voltage out of range"
                self._set_voltage(volts)
                return "OK"
            case "CURR?":
                return f"{self.limit_a:.4f}"
            case "CURR":
                try:
                    amps = float(arg)
                except ValueError:
                    return "ERR bad current"
                if not 0.0 < amps <= _MAX_CURR:
                    return "ERR current out of range"
                self.limit_a = amps
                return "OK"
            case "OUTP?":
                self._tick_protection()
                return "1" if self.output else "0"
            case "OUTP":
                if self.ocp_tripped:
                    return "ERR protection tripped"
                match arg.strip().upper():
                    case "ON":
                        self.output = True
                    case "OFF":
                        self.output = False
                    case _:
                        return "ERR bad output state"
                return "OK"
            case "MEAS:VOLT?":
                return f"{self._measure()[0]:.4f}"
            case "MEAS:CURR?":
                return f"{self._measure()[1]:.4f}"
            case "STAT?":
                volts_amps_mode = self._measure()
                return (
                    f"OUTP={1 if self.output else 0},"
                    f"MODE={volts_amps_mode[2]},OCP={1 if self.ocp_tripped else 0}"
                )
            case "CLPR":
                self.ocp_tripped = False
                self.fault = None
                return "OK"
            case "SIM:LOAD":
                try:
                    ohms = float(arg)
                except ValueError:
                    return "ERR bad load"
                if ohms <= 0:
                    return "ERR load must be positive"
                self.load_ohms = ohms
                return "OK"
            case "SIM:FAULT":
                name = arg.strip().lower()
                if name not in {"ocp", "none"}:
                    return "ERR unknown fault"
                self.fault = None if name == "none" else name
                return "OK"
            case _:
                return "ERR unknown command"
