"""Streaming, cancellation, and interlock recovery over a real WebSocket.

The middle-depth example: `quickstart.py` shows the 60-second happy path,
`demo/` shows a full closed loop: this one shows the operational realities
an agent must handle: live telemetry, a cancel whose halt the hardware
confirms, a cancel that is refused because the operation is already
committed (SPEC 8.3), and recovering from a tripped safety interlock.

Run it (zero hardware):

    uv run examples/streaming.py
"""

import asyncio

from labwire.core import (
    CanceledError,
    ConfirmationRequiredError,
    InstrumentServer,
    InterlockError,
    LabwireClient,
)
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
    # dispense is safety class S2 (SPEC §8.6): the operator issues one token for
    # this session, and every dispense submission must present it.
    grant = "operator-confirmation-example"
    server = InstrumentServer(SyringePump("127.0.0.1", sim.port), confirmation_token=grant)
    async with server.serve_websocket("127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        print(f"instrument server: ws://127.0.0.1:{port}")
        async with await LabwireClient.connect(f"ws://127.0.0.1:{port}") as client:
            stop = asyncio.Event()
            watcher = asyncio.create_task(watch_telemetry(client, stop))

            print("\n1) start a long dispense, then cancel it mid-run")
            print("   (dispense declares cancel_semantics 'abort': the pump has a real STP)")
            handle = await client.submit(
                "dispense", {"volume_ul": 5000.0, "rate_ul_min": 6000.0}, confirmation=grant
            )
            await asyncio.sleep(0.5)
            await handle.cancel()
            try:
                await handle.result(timeout=10.0)
            except CanceledError:
                settled = await handle.status()
                block = settled.cancellation
                assert block is not None
                print(f"  settled: {block.outcome} ({block.detail})")
                # "halted" is a claim the pump EARNED by reporting IDLE after
                # STP; had it not, the record would say "unconfirmed".

            print("\n2) a command that declares cancel 'none' refuses the cancel outright")
            print("   (the PSU setpoint is committed to the device the moment it is sent)")
            from labwire.core import NotCancelableError
            from labwire.drivers import PowerSupply
            from labwire.sim import SimPowerSupply

            psu_sim = SimPowerSupply(seed=7)
            await psu_sim.start()
            psu_server = InstrumentServer(PowerSupply("127.0.0.1", psu_sim.port))
            async with psu_server.serve_websocket("127.0.0.1", 0) as psu_ws:
                psu_port = psu_ws.sockets[0].getsockname()[1]
                async with await LabwireClient.connect(f"ws://127.0.0.1:{psu_port}") as psu:
                    await (await psu.submit("output", {"on": True})).result(timeout=10.0)
                    slew = await psu.submit("set_voltage", {"volts": 24.0})
                    await asyncio.sleep(0.2)  # mid-slew
                    try:
                        await slew.cancel()
                    except NotCancelableError as exc:
                        print(f"  refused: {exc}")
                        print(f"  details: {exc.details}")
                    result = await slew.result(timeout=30.0)
                    print(f"  the slew finished anyway, honestly: {result['volts']:.2f} V")
            await psu_server.aclose()
            await psu_sim.stop()

            print("\n3) an S2 command without confirmation is refused outright")
            try:
                await client.submit("dispense", {"volume_ul": 1.0, "rate_ul_min": 6000.0})
            except ConfirmationRequiredError as exc:
                print(f"  refused: {exc}")

            print("\n4) inject an occlusion fault and watch the interlock trip")
            inject = await client.submit("x-sim/inject_fault", {"kind": "occlusion"})
            await inject.result(timeout=10.0)
            stalled = await client.submit(
                "dispense", {"volume_ul": 500.0, "rate_ul_min": 6000.0}, confirmation=grant
            )
            try:
                await stalled.result(timeout=10.0)
            except InterlockError as exc:
                print(f"  run failed as designed: {exc}")
            try:
                await client.submit(
                    "dispense", {"volume_ul": 10.0, "rate_ul_min": 6000.0}, confirmation=grant
                )
            except InterlockError:
                print("  submits are blocked while the interlock is tripped")

            print("\n5) recover with the declared clearing command and finish a run")
            clearing = await client.submit("clear_occlusion", {})
            await clearing.result(timeout=10.0)
            retry = await client.submit(
                "dispense", {"volume_ul": 100.0, "rate_ul_min": 60000.0}, confirmation=grant
            )
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
