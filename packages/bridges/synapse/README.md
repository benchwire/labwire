# labwire-synapse (EXPERIMENTAL)

Expose a [Science Corp Synapse](https://github.com/sciencecorp/synapse-api)
device as a Labwire instrument: typed commands with UCUM units, S0-S3 safety
classes with confirmation and operator grants, declared cancel semantics, a
device resource for discovery, reduced telemetry, and signed run manifests.

> **EXPERIMENTAL, branch `synapse-bridge` only. Nothing here is published to
> PyPI**, and that is enforced rather than asserted: the release workflow
> publishes an explicit matrix of project names, and `labwire-synapse` is not
> in it. Verified against the `synapse-sim` simulator that ships with
> `science-synapse` and against **no hardware of any kind**. No compatibility
> with any Science Corp device is claimed. Intended for research rigs, not for
> clinical or implanted use. Read [SYNAPSE.md](SYNAPSE.md) before using it for
> anything.

## What it is

One Synapse device becomes one Labwire instrument. The bridge speaks the
Synapse gRPC control plane (port 647 by default) through the official
`science-synapse` Python client, and reduces the out-of-band ZeroMQ data taps
into a handful of derived telemetry channels.

`science-synapse` is an **optional** dependency. The package imports cleanly
without it; you need it to have a device to pass in.

```bash
uv pip install "science-synapse>=2.7,<3"
```

## Five-minute run, no hardware

```bash
# 1. the simulator that ships with science-synapse
synapse-sim --iface-ip 127.0.0.1 --rpc-port 50647 --name "bench-1" --serial "SIM-0001"
```

```python
import asyncio
from pathlib import Path

import synapse
from labwire.bridges.synapse import SynapseInstrument
from labwire.core import InstrumentServer

async def main() -> None:
    instrument = SynapseInstrument(synapse.Device("127.0.0.1:50647"))
    # A grant store is REQUIRED: this instrument declares S3 commands, and
    # InstrumentServer refuses to construct without somewhere to verify grants.
    server = InstrumentServer(
        instrument,
        grant_store=Path("grants"),
        confirmation_token="operator-confirmation",
    )
    await server.serve_websocket("127.0.0.1", 9520)

asyncio.run(main())
```

## The command surface

| Command | Class | Cancel | Notes |
|---|---|---|---|
| `get_info` | S0 | none | Pure read; the diagnostic that stays available while interlocked |
| `list_taps` | S1 | none | Synapse refuses `Query` unless the device is running |
| `get_settings` | S1 | none | |
| `self_test` | S1 | none | Class is a judgement about the query, not a guarantee about the hardware |
| `measure_impedance` | **S2** | none | Injects a test current, so it is not a passive read |
| `configure_broadband` | S1 | none | `Hz`, `bit`, `1` |
| `configure_filter` | S1 | none | `Hz` |
| `configure_spike_detect` | S1 | none | `uV`, refuses a fractional threshold |
| `configure_spike_binner` | S1 | none | `ms`, refuses a fractional bin |
| `configure_optical_stimulation` | **S3** | none | Operator grant, bound to these parameter values |
| `configure_electrical_stimulation` | **S3** | none | Declared and gated; the simulator refuses the node type |
| `clear_signal_chain` | S0 | none | The path that removes a stimulation node, so it stays submittable |
| `start_acquisition` | S1 | none | Refuses to start a chain containing a stimulation node |
| `start_stimulation` | **S3** | none | The call that energizes; its own grant |
| `apply_chain_and_start` | S1 | between_steps | Configure, boundary, Start |
| `stop` | **S0** | none | Recovery path; its result is the device's claim, relayed, not verified |

`cancel_semantics` is `"none"` everywhere except `apply_chain_and_start`.
Synapse has no cancel or abort RPC at all, and every call the bridge makes is
committed the moment it is issued. `apply_chain_and_start` is the one command
the bridge sequences itself, so it is the one command that can honestly stop
at a boundary.

## Telemetry: the interesting part

A `BroadbandSource` publishes **one ZeroMQ message per sample instant**. At
30 kHz that is thirty thousand messages per second, measured here at 29,794
frames/s against the simulator. That cannot go through Labwire channels and
the bridge does not try. A worker thread drains the tap and keeps counters;
the event loop publishes once per window:

- `samples_received` (`1`): frames **this bridge received**, not frames the
  device produced.
- `frames_dropped` (`1`): gaps in the tap's own `sequence_number`, so the
  reduction's own lossiness is visible.
- `sample_rate_measured_hz` (`Hz`): arrival rate.
- `rms_counts` (`1`): RMS in raw **ADC counts**.
- `rms_uV` (`uV`): declared **only** when the device reported `lsb_uV` at
  startup. Without that scale, counts cannot be converted to microvolts, and
  the bridge publishes counts rather than inventing a factor. The simulator
  never reports it, so this channel does not exist against the simulator.

## What Labwire adds that Synapse does not have

Units on every quantity; a risk class on every command; a confirmation gate on
impedance; an operator-grant gate on stimulation, bound to the exact parameter
digest, expiring and use-limited; declared cancel semantics with honest
settlement; a typed device resource for discovery; and an ed25519-signed run
manifest of what was commanded.

It does not add safety. Synapse's stimulation configs carry no amplitude,
charge, or duty limits, so there is nothing there for a protocol to bound. The
honest claim is that stimulation through this bridge is **gated,
parameter-bound, and recorded**.

## Licensing note

`science-synapse` (the Python client) is Apache-2.0. `synapse-api` (the
protobuf definitions the client is generated from) ships with no license file
and a `COPYRIGHT` stating all rights reserved. This bridge vendors neither and
depends only on the published `science-synapse` distribution.

## Tests

```bash
uv sync --all-packages
uv pip install "science-synapse>=2.7,<3"
uv run --no-sync pytest packages/bridges/synapse/tests -v
```

The tests start `synapse-sim` as a subprocess on an ephemeral port and drive
the bridge through a real `InstrumentServer` and `LabwireClient` over
`MemoryTransport`. Without `science-synapse` installed, the whole module skips.
