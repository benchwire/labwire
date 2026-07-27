"""`make demo`: a scripted optimizer closes the loop over three instruments.

Optimizes reaction yield over heater voltage (temperature) and reagent flow
rate using the simulated power supply, syringe pump, and balance, live
telemetry over real WebSocket connections, ending with an ed25519-signed
results bundle verified on the spot.

Run:
    uv run python examples/demo/closed_loop.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from labwire.core import verify_bundle
from rig import RATE_RANGE, STANDING_GRANT, VOLT_RANGE, DemoRig, ExperimentResult


async def optimize(rig: DemoRig, budget: int) -> list[ExperimentResult]:
    """Two-stage coordinate search: coarse 3x3 grid, then refine around the best."""
    results: list[ExperimentResult] = []

    async def measure(volts: float, rate: float) -> ExperimentResult:
        result = await rig.run_experiment(volts=volts, rate_ul_min=rate)
        results.append(result)
        best = max(results, key=lambda r: r.product_g)
        print(
            f"  run {len(results):02d}  V={result.volts:5.1f} V -> T={result.temp_c:5.1f} degC"
            f"   q={result.rate_ul_min:5.0f} uL/min"
            f"   yield={result.yield_pct:5.1f}%   best={best.yield_pct:5.1f}%"
        )
        return result

    v_lo, v_hi = VOLT_RANGE
    q_lo, q_hi = RATE_RANGE
    coarse_v = [v_lo + (v_hi - v_lo) * f for f in (0.17, 0.5, 0.83)]
    coarse_q = [q_lo + (q_hi - q_lo) * f for f in (0.17, 0.5, 0.83)]
    print("phase 1: coarse 3x3 grid")
    for volts in coarse_v:
        for rate in coarse_q:
            if len(results) >= budget:
                break
            await measure(volts, rate)

    best = max(results, key=lambda r: r.product_g)
    v_span = (v_hi - v_lo) / 4
    q_span = (q_hi - q_lo) / 4
    print("phase 2: refine around the best point")
    for dv, dq in [(-1, 0), (1, 0), (0, -1), (0, 1), (0, 0)]:
        if len(results) >= budget:
            break
        volts = min(max(best.volts + dv * v_span / 2, v_lo), v_hi)
        rate = min(max(best.rate_ul_min + dq * q_span / 2, q_lo), q_hi)
        if dv == 0 and dq == 0:
            best = max(results, key=lambda r: r.product_g)
            volts, rate = best.volts, best.rate_ul_min
        await measure(volts, rate)

    return results


async def main() -> None:
    """Run the closed-loop optimization and verify the signed evidence."""
    fast = os.environ.get("DEMO_FAST") == "1"
    budget = 4 if fast else 14
    manifest_dir = Path(os.environ.get("DEMO_RUNS_DIR", "demo_runs"))
    print("labwire closed-loop demo: maximize reaction yield over (voltage, flow rate)")
    print("instruments: SimPSU-3005 (heater), SimPump-200 (reagent), SimBalance-120 (product)")
    print(
        f"safety:      pump dispense is class S2 (irreversible); running under the "
        f"operator standing grant {STANDING_GRANT!r}"
    )
    async with await DemoRig.start(manifest_dir, time_scale=240.0 if fast else 60.0) as demo_rig:
        results = await optimize(demo_rig, budget)
        best = max(results, key=lambda r: r.product_g)
        print(
            f"\nconverged: best yield {best.yield_pct:.1f}% at "
            f"{best.volts:.1f} V ({best.temp_c:.1f} degC), {best.rate_ul_min:.0f} uL/min "
            f"in {len(results)} experiments"
        )
        bundle = demo_rig.latest_bundle()
        assert bundle is not None, "no signed bundle was produced"
        outcome = verify_bundle(bundle)
        print(f"\nsigned evidence: {bundle}")
        print(f"  labwire verify: {'OK - authentic' if outcome.ok else outcome.errors}")
        if not outcome.ok:
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
