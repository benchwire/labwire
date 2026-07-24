"""Streaming, cancellation, and interlock recovery over a real WebSocket.

The middle-depth example: `quickstart.py` shows the 60-second happy path,
`demo/` shows a full closed loop — this one shows the operational realities
an agent must handle: live telemetry, cancelling a long run, and recovering
from a tripped safety interlock.

Run it (zero hardware):

    uv run examples/streaming.py
"""

import asyncio

from labwire.core import CanceledError, InstrumentServer, InterlockError, LabwireClient
from labwire.drivers import SyringePump
from labwire.sim import SimSyringePump


async def watch_telemetry(client: LabwireClient, stop: asyncio.Event) -> None:
    """Print flow-rate samples as they stream."""
    async with client.telemetry(["flow_rate"], max_rate_hz=5.0) as subscription:
        async for sample in subscription:
            print(f"  telemetry: flow_rate = {sample.value:7.1f} uL/min  (seq {sample.seq})")
            if stop.is_set():
                return


async def main() -> None:
    """Drive a pump over WebSocket: stream, cancel, trip, recover."""
    sim = SimSyringePump(seed=42)
    await sim.start()
    server = InstrumentServer(SyringePump("127.0.0.1", sim.port))
    async with server.serve_websocket("127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        print(f"instrument server: ws://127.0.0.1:{port}")
        async with await LabwireClient.connect(f"ws://127.0.0.1:{port}") as client:
            stop = asyncio.Event()
            watcher = asyncio.create_task(watch_telemetry(client, stop))

            print("\n1) start a long dispense, then cancel it mid-run")
            handle = await client.submit("dispense", {"volume_ul": 5000.0, "rate_ul_min": 6000.0})
            await asyncio.sleep(0.5)
            await handle.cancel()
            try:
                await handle.result(timeout=10.0)
            except CanceledError:
                print("  canceled cleanly; motor stopped")

            print("\n2) inject an occlusion fault and watch the interlock trip")
            inject = await client.submit("x-sim/inject_fault", {"kind": "occlusion"})
            await inject.result(timeout=10.0)
            stalled = await client.submit("dispense", {"volume_ul": 500.0, "rate_ul_min": 6000.0})
            try:
                await stalled.result(timeout=10.0)
            except InterlockError as exc:
                print(f"  run failed as designed: {exc}")
            try:
                await client.submit("dispense", {"volume_ul": 10.0, "rate_ul_min": 6000.0})
            except InterlockError:
                print("  submits are blocked while the interlock is tripped")

            print("\n3) recover with the declared clearing command and finish a run")
            clearing = await client.submit("clear_occlusion", {})
            await clearing.result(timeout=10.0)
            retry = await client.submit("dispense", {"volume_ul": 100.0, "rate_ul_min": 60000.0})
            result = await retry.result(timeout=10.0)
            print(f"  dispensed {result['dispensed_ul']:.1f} uL after recovery")

            stop.set()
            await asyncio.sleep(0.1)
            watcher.cancel()
    await server.aclose()
    await sim.stop()
    print("\ndone")


if __name__ == "__main__":
    asyncio.run(main())
