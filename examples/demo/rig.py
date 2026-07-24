"""The closed-loop demo rig: three simulated instruments plus chemistry.

A simulated flow reaction: the power supply drives a heater (voltage sets
reactor temperature), the syringe pump feeds reagent (flow rate sets
residence time), and the analytical balance weighs the product collected.
Yield peaks at a hidden optimum; the demo harness computes the chemistry
between devices — placing product mass on the balance after each dispense —
while every device interaction goes through the real Labwire protocol over
WebSocket.
"""

import math
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from labwire.core import InstrumentServer, LabwireClient
from labwire.drivers import Balance, PowerSupply, SyringePump
from labwire.sim import ScaledClock, SimBalance, SimPowerSupply, SimSyringePump

DISPENSE_UL = 40.0
VOLT_RANGE = (5.0, 25.0)
RATE_RANGE = (80.0, 240.0)

_OPT_TEMP_C = 65.0
_OPT_RATE = 120.0


def reactor_temp_c(volts: float) -> float:
    """Heater response: reactor temperature as a function of PSU voltage."""
    return 20.0 + 3.2 * volts


def yield_fraction(temp_c: float, rate_ul_min: float) -> float:
    """Hidden yield surface with a single optimum (T=65 °C, q=120 uL/min)."""
    temp_term = math.exp(-(((temp_c - _OPT_TEMP_C) / 14.0) ** 2))
    rate_term = math.exp(-(((rate_ul_min - _OPT_RATE) / 70.0) ** 2))
    return 0.93 * temp_term * rate_term


@dataclass
class ExperimentResult:
    """One closed-loop experiment: setpoints in, measured product out."""

    volts: float
    temp_c: float
    rate_ul_min: float
    product_g: float

    @property
    def yield_pct(self) -> float:
        """Measured yield as a percentage of the theoretical maximum mass."""
        return 100.0 * self.product_g / (DISPENSE_UL * 0.005)


class DemoRig:
    """Owns the sims, servers, and clients; runs one experiment at a time.

    Example:
        >>> # async with DemoRig.start(Path("demo_runs")) as rig:
        >>> #     result = await rig.run_experiment(volts=14.0, rate_ul_min=120.0)
    """

    def __init__(self) -> None:
        self.clock = ScaledClock(60.0)  # 1 real second = 1 simulated minute
        self.psu_client: LabwireClient
        self.pump_client: LabwireClient
        self.balance_client: LabwireClient
        self.manifest_dir: Path
        self._stack: AsyncExitStack

    @classmethod
    async def start(cls, manifest_dir: Path, *, time_scale: float = 60.0) -> Self:
        """Boot sims and servers, connect clients; use with ``async with``."""
        rig = cls()
        rig.clock = ScaledClock(time_scale)
        rig.manifest_dir = manifest_dir
        stack = AsyncExitStack()
        rig._stack = stack

        sims = [
            SimPowerSupply(seed=11, clock=rig.clock),
            SimSyringePump(seed=12, clock=rig.clock),
            SimBalance(seed=13, clock=rig.clock),
        ]
        for sim in sims:
            await sim.start()
            stack.push_async_callback(sim.stop)
        psu_sim, pump_sim, balance_sim = sims

        drivers = [
            PowerSupply("127.0.0.1", psu_sim.port),
            SyringePump("127.0.0.1", pump_sim.port),
            Balance("127.0.0.1", balance_sim.port),
        ]
        clients: list[LabwireClient] = []
        for index, driver in enumerate(drivers):
            server = InstrumentServer(
                driver,
                clock=rig.clock,
                # the balance produces the signed evidence for each measurement
                manifest_dir=manifest_dir if index == 2 else None,
            )
            stack.push_async_callback(server.aclose)
            ws_server = await stack.enter_async_context(server.serve_websocket("127.0.0.1", 0))
            port = ws_server.sockets[0].getsockname()[1]
            client = await LabwireClient.connect(f"ws://127.0.0.1:{port}")
            await stack.enter_async_context(client)
            clients.append(client)
        rig.psu_client, rig.pump_client, rig.balance_client = clients
        return rig

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.aclose()

    async def _run(self, client: LabwireClient, name: str, params: dict[str, Any]) -> Any:
        handle = await client.submit(name, params)
        return await handle.result(timeout=120.0)

    async def run_experiment(self, *, volts: float, rate_ul_min: float) -> ExperimentResult:
        """One iteration: heat, dispense, react, weigh — all over the wire.

        Example:
            >>> # result = await rig.run_experiment(volts=14.0, rate_ul_min=120.0)
        """
        await self._run(self.psu_client, "set_current_limit", {"amps": 2.0})
        await self._run(self.psu_client, "output", {"on": True})
        settled = await self._run(self.psu_client, "set_voltage", {"volts": volts})
        temp_c = reactor_temp_c(float(settled["volts"]))

        await self._run(
            self.pump_client,
            "dispense",
            {"volume_ul": DISPENSE_UL, "rate_ul_min": rate_ul_min},
        )
        # chemistry: the reaction converts reagent to product at this (T, q)
        product_g = DISPENSE_UL * 0.005 * yield_fraction(temp_c, rate_ul_min)

        await self._run(self.balance_client, "x-sim/load", {"mass_g": 0.0})  # empty the vessel
        await self._run(self.balance_client, "tare", {})
        await self._run(self.balance_client, "x-sim/load", {"mass_g": product_g})
        measured = await self._run(self.balance_client, "measure", {})
        return ExperimentResult(
            volts=volts,
            temp_c=temp_c,
            rate_ul_min=rate_ul_min,
            product_g=float(measured["mass_g"]),
        )

    def latest_bundle(self) -> Path | None:
        """The most recently written signed bundle directory, if any."""
        bundles = sorted(
            (p for p in self.manifest_dir.iterdir() if (p / "manifest.json").exists()),
            key=lambda p: (p / "manifest.json").stat().st_mtime,
        )
        return bundles[-1] if bundles else None
