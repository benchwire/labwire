"""Simulated analytical balance with realistic settling behavior.

An original Labwire device model ("SimBalance-120", 120 g capacity) — not an
emulation of any real vendor's balance or protocol. Serial-style line
protocol over TCP:

    VER?               -> LabwireBalance,SimBalance-120,SN-B1200003,0.1.0
    SI                 -> S D <val> g   (dynamic)  |  S S <val> g  (stable)
                          S +           (overload)
    T                  -> T OK <val> g  (tare; requires stability)
    SIM:LOAD <g>       -> OK            (simulation-only: place mass on pan)
    SIM:FAULT <name>   -> OK            (simulation-only: vibration | none)

Realism: readings settle exponentially after a load change, carry gaussian
noise, stability requires a quiet settling window, vibration destroys it.

Example:
    >>> from labwire.sim import SimBalance
    >>> # sim = SimBalance(seed=3); await sim.start()
"""

import asyncio
import math
import random
from datetime import datetime

from labwire.core import Clock, SystemClock

_TAU_S = 0.10  # settling time constant
_SETTLE_S = 0.35  # quiet time after a load change before readings are stable
_NOISE_G = 0.0004
_CAPACITY_G = 120.0


class SimBalance:
    """A simulated analytical balance TCP server.

    Example:
        >>> sim = SimBalance(seed=3)
        >>> # await sim.start(); ...; await sim.stop()
    """

    def __init__(self, *, seed: int = 0, clock: Clock | None = None) -> None:
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.rng = random.Random(seed)
        self.serial_number = f"SN-B{1200000 + seed:07d}"
        self.gross_g = 0.0
        self.tare_g = 0.0
        self.fault: str | None = None
        self._previous_gross_g = 0.0
        self._changed_at: datetime = self.clock.now()
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

    def _elapsed_s(self) -> float:
        return (self.clock.now() - self._changed_at).total_seconds()

    def _reading_g(self) -> float:
        settling = (self._previous_gross_g - self.gross_g) * math.exp(-self._elapsed_s() / _TAU_S)
        noise_scale = 50.0 if self.fault == "vibration" else 1.0
        noise = self.rng.gauss(0.0, _NOISE_G * noise_scale)
        return self.gross_g + settling + noise

    def _stable(self) -> bool:
        if self.fault == "vibration":
            return False
        residual = abs(self._previous_gross_g - self.gross_g) * math.exp(
            -self._elapsed_s() / _TAU_S
        )
        return self._elapsed_s() >= _SETTLE_S and residual < 0.001

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                await self.clock.sleep(self.rng.uniform(0.002, 0.006))  # command latency
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
            case "VER?":
                return f"LabwireBalance,SimBalance-120,{self.serial_number},0.1.0"
            case "SI":
                if self.gross_g > _CAPACITY_G:
                    return "S +"
                marker = "S" if self._stable() else "D"
                return f"S {marker} {self._reading_g() - self.tare_g:.4f} g"
            case "T":
                if self.gross_g > _CAPACITY_G:
                    return "T ERR overload"
                if not self._stable():
                    return "T ERR unstable"
                self.tare_g = self._reading_g()
                return f"T OK {self.tare_g:.4f} g"
            case "SIM:LOAD":
                try:
                    grams = float(arg)
                except ValueError:
                    return "ERR bad mass"
                self._previous_gross_g = min(self.gross_g, _CAPACITY_G)
                self.gross_g = grams
                self._changed_at = self.clock.now()
                return "OK"
            case "SIM:FAULT":
                name = arg.strip().lower()
                if name not in {"vibration", "none"}:
                    return "ERR unknown fault"
                self.fault = None if name == "none" else name
                return "OK"
            case _:
                return "ERR unknown command"
