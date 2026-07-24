"""Simulated syringe pump speaking a serial-style line protocol over TCP.

An original Labwire device model ("SimPump-200") — not an emulation of any
real vendor's pump. The protocol is newline-terminated ASCII, in the style
of bench serial pumps:

    VER?                 -> LabwirePump,SimPump-200,SN-P2000042,0.1.0
    RAT <uL_per_min>     -> OK
    VOL <uL>             -> OK
    RUN                  -> OK | ERR ...
    STP                  -> OK
    STAT?                -> <IDLE|RUN|STALL>,RATE=<r>,DISP=<d>,TARG=<t>
    CLF                  -> OK          (clear fault/stall)
    SIM:FAULT <name>     -> OK          (simulation-only: occlusion | none)

Realism: command latency, ~1% flow-rate jitter, occlusion stalls the motor
shortly after RUN.

Example:
    >>> from labwire.sim import SimSyringePump
    >>> # sim = SimSyringePump(seed=1); await sim.start(); sim.port
"""

import asyncio
import contextlib
import random

from labwire.core import Clock, SystemClock

_TICK_S = 0.02
_OCCLUSION_STALL_AFTER_S = 0.15


class SimSyringePump:
    """A simulated syringe pump TCP server.

    Example:
        >>> sim = SimSyringePump(seed=1)
        >>> # await sim.start(); ...; await sim.stop()
    """

    def __init__(self, *, seed: int = 0, clock: Clock | None = None) -> None:
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.rng = random.Random(seed)
        self.serial_number = f"SN-P{2000000 + seed:07d}"
        self.state = "IDLE"  # IDLE | RUN | STALL
        self.rate_ul_min = 0.0
        self.target_ul = 0.0
        self.dispensed_ul = 0.0
        self.fault: str | None = None
        self._server: asyncio.Server | None = None
        self._motor: asyncio.Task[None] | None = None
        self._run_elapsed_s = 0.0

    @property
    def port(self) -> int:
        """The ephemeral TCP port the simulator listens on."""
        assert self._server is not None, "call start() first"
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        """Start listening on 127.0.0.1 (ephemeral port)."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    async def stop(self) -> None:
        """Stop the motor and the TCP server."""
        if self._motor is not None:
            self._motor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._motor
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                await self.clock.sleep(self.rng.uniform(0.002, 0.008))  # command latency
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
                return f"LabwirePump,SimPump-200,{self.serial_number},0.1.0"
            case "RAT":
                try:
                    rate = float(arg)
                except ValueError:
                    return "ERR bad rate"
                if not 0.0 < rate <= 120000.0:
                    return "ERR rate out of range"
                self.rate_ul_min = rate
                return "OK"
            case "VOL":
                try:
                    self.target_ul = float(arg)
                except ValueError:
                    return "ERR bad volume"
                return "OK"
            case "RUN":
                if self.state == "STALL":
                    return "ERR stalled"
                if self.rate_ul_min <= 0 or self.target_ul <= 0:
                    return "ERR rate/volume not set"
                self.dispensed_ul = 0.0
                self._run_elapsed_s = 0.0
                self.state = "RUN"
                self._motor = asyncio.create_task(self._run_motor())
                return "OK"
            case "STP":
                self.state = "IDLE" if self.state != "STALL" else "STALL"
                return "OK"
            case "STAT?":
                return (
                    f"{self.state},RATE={self.rate_ul_min:.1f},"
                    f"DISP={self.dispensed_ul:.2f},TARG={self.target_ul:.2f}"
                )
            case "CLF":
                self.fault = None
                if self.state == "STALL":
                    self.state = "IDLE"
                return "OK"
            case "SIM:FAULT":
                name = arg.strip().lower()
                if name not in {"occlusion", "none"}:
                    return "ERR unknown fault"
                self.fault = None if name == "none" else name
                return "OK"
            case _:
                return "ERR unknown command"

    async def _run_motor(self) -> None:
        while self.state == "RUN":
            await self.clock.sleep(_TICK_S)
            if self.state != "RUN":
                return  # stopped while sleeping: a dead motor must not stall
            self._run_elapsed_s += _TICK_S
            if self.fault == "occlusion" and self._run_elapsed_s >= _OCCLUSION_STALL_AFTER_S:
                self.state = "STALL"  # pressure built up: motor stalls
                return
            jitter = 1.0 + self.rng.gauss(0.0, 0.01)
            self.dispensed_ul += (self.rate_ul_min / 60.0) * _TICK_S * jitter
            if self.dispensed_ul >= self.target_ul:
                # small terminal over/undershoot, as a real stepper exhibits
                self.dispensed_ul = self.target_ul * (1.0 + self.rng.gauss(0.0, 0.003))
                self.state = "IDLE"
                return
