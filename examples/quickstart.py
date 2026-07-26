"""Labwire in 60 seconds: a simulated balance, driven end to end.

Run it (zero hardware, zero configuration):

    uv run examples/quickstart.py

It defines a simulated analytical balance, serves it over an in-process
transport, then acts as an AI agent's client: discovers the instrument,
subscribes to telemetry, runs a measurement, and prints the signed-ready
run record.
"""

import asyncio

from labwire.core import (
    CommandContext,
    IdentityInfo,
    Instrument,
    InstrumentServer,
    LabwireClient,
    MemoryTransport,
    TelemetrySubscription,
    channel,
    command,
)


class SimBalance(Instrument):
    """A tiny simulated analytical balance."""

    identity = IdentityInfo(
        manufacturer="Labwire Project",
        model="SimBalance-120",
        serial_number="SIM-0003",
        firmware_version="0.1.0",
    )

    mass = channel("mass", unit="g", description="Current mass reading.")

    @command(
        units={"settle_s": "s"},
        returns_units={"mass_g": "g"},
        estimated_duration_s=2.0,
    )
    async def measure(self, ctx: CommandContext, settle_s: float = 0.5) -> dict[str, float]:
        """Let the reading stabilize, streaming samples, then report the mass."""
        reading = 0.0
        for step in range(1, 6):
            reading = 12.3456 * (1 - 0.5**step)  # converge toward the true mass
            self.mass.publish(round(reading, 4))
            await ctx.progress(step / 5, f"settling ({step}/5)")
            await ctx.sleep(settle_s / 5)
        ctx.emit_event("measurement/stable", "info", {"channel": "mass", "value": reading})
        return {"mass_g": round(reading, 4)}


async def watch_telemetry(subscription: TelemetrySubscription) -> None:
    """Print samples as they stream in."""
    async for sample in subscription:
        print(f"  telemetry:  mass = {sample.value} g (seq {sample.seq})")


async def main() -> None:
    """Serve the balance in-process and drive it like an agent would."""
    server = InstrumentServer(SimBalance())
    client_end, server_end = MemoryTransport.pair()
    server.attach(server_end)

    async with LabwireClient.attach(client_end) as client:
        descriptor = await client.describe()
        print(f"connected to: {descriptor.identity.manufacturer} {descriptor.identity.model}")
        print(f"commands:     {[c.name for c in descriptor.commands]}")
        print(f"channels:     {[c.name for c in descriptor.channels]}")

        async with client.telemetry(["mass"]) as subscription:
            watcher = asyncio.create_task(watch_telemetry(subscription))
            handle = await client.submit("measure", {"settle_s": 0.5})
            print(f"submitted:    measure (command_id {handle.command_id[:8]}…)")
            result = await handle.result(timeout=30)
        await watcher  # the subscription ended its stream; watcher drains and exits
        final = await handle.status()
        print(f"terminal:     {final.status}")
        print(f"result:       {result}")

        record = server.run_records[handle.command_id]
        print(f"run record:   status={record.status} digest=sha256:{record.digest[:16]}…")
        print("(run `make demo` to see runs like this become ed25519-signed, verifiable bundles)")


if __name__ == "__main__":
    asyncio.run(main())
