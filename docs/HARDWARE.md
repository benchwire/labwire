# Connecting real instruments

**Read the status line first: real transports, tested against simulators,
awaiting hardware.** Everything below has been exercised against this
repository's simulated instruments over real TCP sockets and a PTY-backed
serial responder, and never against a physical instrument. No labwire
driver claims compatibility with any vendor model. This page is the
walkthrough to follow the day the equipment arrives, written so the gaps
you will hit are named where you will hit them.

## What you need

- The instrument's programming manual (the chapter listing its SCPI or
  line-protocol commands). Nothing here can substitute for it.
- A checkout with `make setup` done, or `pip install labwire` plus
  `pip install 'labwire-drivers[serial]'` if the instrument is USB-serial.
- The endpoint: either `HOST:PORT` for LAN/SCPI-over-TCP instruments
  (port 5025 is the conventional SCPI raw socket), or the serial device
  path (`/dev/tty.usbserial-XXXX` on macOS, `/dev/ttyUSB0` on Linux).

## Step 1: probe the endpoint

```bash
labwire probe 10.0.0.5:5025
# or
labwire probe --serial /dev/tty.usbserial-A50 --baud 9600
```

`probe` opens the link, asks `*IDN?`, and writes `<model>.yaml`: a draft
annotation with the identity the instrument reported and a TODO for
everything it could not learn. If the probe hangs or the reply is not a
comma-separated identity, the instrument likely does not speak SCPI on
that endpoint; check the manual for the right port or protocol before
going further.

What the draft deliberately does not do: pick a driver for you. That
decision is Step 2, and it is a judgment call about command sets, not
something a `*IDN?` string can settle.

## Step 2: match a driver, or admit there is none

Open the manual's command table next to the driver you hope matches:

| Driver | Speaks | Developed against |
|---|---|---|
| `labwire.drivers:PowerSupply` | SCPI-style PSU commands (`VOLT`, `MEAS:VOLT?`, `OUTP`) | `labwire.sim.SimPowerSupply` |
| `labwire.drivers:Balance` | line-protocol balance commands (`VER?`, weight polling) | `labwire.sim.SimBalance` |
| `labwire.drivers:SyringePump` | line-protocol pump commands | `labwire.sim.SimSyringePump` |

The drivers were written against the simulators' command sets, which
imitate common conventions; a real instrument will differ somewhere.
Expect at minimum: different status registers, different error replies,
and vendor-specific settling behavior. When a command differs, the honest
move is a new driver (subclass or copy), not a compatibility shim that
half-works. If nothing matches, you are writing a driver; the three in
`packages/drivers` are small and are the template.

## Step 3: declare the deployment

One file says which driver speaks to which endpoint
(`labwire-instruments.yaml`, format enforced by
`labwire.drivers.load_endpoints`, unknown keys are errors):

```yaml
version: 1
instruments:
  psu:
    driver: labwire.drivers:PowerSupply
    transport: tcp
    address: "10.0.0.5:5025"
  balance:
    driver: labwire.drivers:Balance
    transport: serial
    device: /dev/tty.usbserial-A50
    baud: 9600
    annotation: SimBalance-120.yaml   # the probe draft, TODOs resolved
```

Keep the probe drafts next to it, TODOs resolved from the manual. These
files are deployment truth, like grant stores: they live with the bench,
not in this repository.

## Step 4: serve

```python
import asyncio
from pathlib import Path

from labwire.core import InstrumentServer
from labwire.drivers import load_endpoints


async def main() -> None:
    endpoints = load_endpoints(Path("labwire-instruments.yaml"))
    psu = next(e for e in endpoints if e.name == "psu")
    server = InstrumentServer(
        psu.instrument(),
        manifest_dir=Path("runs"),        # signed evidence per run
        confirmation_token="CHANGE-ME",   # the S2 standing confirmation
        grant_store=Path("grants"),       # only if S3 commands exist
    )
    async with server.serve_websocket("127.0.0.1", 9520):
        await asyncio.Future()


asyncio.run(main())
```

Serve on loopback until you trust the whole chain; anything crossing a
network boundary should be `wss://` behind your own authentication
(SECURITY.md says precisely what the protocol does and does not provide).

## Step 5: prove it behaves

```bash
labwire-conformance ws://127.0.0.1:9520
```

The conformance suite never executes a command uninvited, so this is safe
against a live bench. When you are ready to prove the full lifecycle, pick
the safest command the instrument has and opt in:

```bash
labwire-conformance ws://127.0.0.1:9520 \
  --exercise measure --params '{}' --bundle-dir runs --claim signed
```

## Known gaps, named honestly

- **The first real instrument WILL find driver bugs.** The drivers have
  only ever spoken to simulators that imitate SCPI conventions; where a
  vendor deviates, the driver is wrong until fixed. File the bug with the
  manual page attached.
- **Serial on Windows is unverified.** The serial transport uses
  pyserial-asyncio-fast, whose asyncio integration is POSIX-oriented; on
  Windows prefer the instrument's TCP endpoint. TODO-VERIFY:
  pyserial-asyncio-fast 0.16 behavior on Windows.
- **No flow control, framing, or checksum options yet.** The serial link
  is 8N1, newline-terminated lines. Balances that stream unsolicited
  weights, binary-block transfers, and RTS/CTS handshaking are not
  handled; they will need transport work, not configuration.
- **`labwire probe` only speaks `*IDN?`.** Instruments that need a wake-up
  sequence, a different terminator, or address selection (RS-485) will
  time out; probe them manually with the manual open.
- **Timeouts are conservative defaults, not tuned.** A slow-settling
  instrument may need driver-level patience a simulator never taught us.
