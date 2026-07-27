# labwire-pylabrobot

**Expose a [PyLabRobot](https://github.com/PyLabRobot/pylabrobot) liquid
handler as a Labwire instrument**, so an AI agent can read a deck, plan
transfers, and move liquid through one protocol, with the units, safety
classes, and signed results PyLabRobot does not carry.

PyLabRobot abstracts liquid handling *for Python programmers*. Labwire
describes instruments *to AI agents*. This bridge composes them; Labwire does
not reimplement drivers.

It also exists to answer a harder question than the ophyd bridge could. ophyd
devices are signal-shaped, which is the shape Labwire v0.2 was designed
around. Liquid handling is not: its commands act on things, and its
interesting state is a tree. Everywhere the protocol strained is written down
in [SPEC-FINDINGS.md](../../../SPEC-FINDINGS.md), which is the real output of
this package.

## Five-minute quickstart

Nothing here needs hardware, a server, or a browser. From a checkout with
`make setup` done:

```bash
# 1. See what Labwire would serve from a deck
uv run labwire-pylabrobot check \
  examples.liquid_handling.rig:build_liquid_handler \
  -a examples/liquid_handling/labwire-pylabrobot.yaml

# 2. Watch a serial dilution run through the protocol, with signed evidence
make demo-pylabrobot

# 3. The same dilution, planned by a Claude agent (needs ANTHROPIC_API_KEY)
make demo-pylabrobot-claude
```

`check` prints the resolved instrument: every command with its safety class,
every piece of labware with what the annotation file says it holds.

```
OK: LiquidHandlerChatterboxBackend (lh_deck)
  8 channel(s), 8 piece(s) of labware
    S0  describe_deck
    S2  aspirate
    S2  dispense
    S1  set_well_volume
    S0  stop
    tips: tip_rack 8x12  (96 tips)
    source_plate: plate 8x12  (hazard: none)
```

Serving it is a few lines:

```python
from labwire.bridges.pylabrobot import PyLabRobotInstrument, load_annotations
from labwire.core import InstrumentServer

instrument = PyLabRobotInstrument(handler, load_annotations(Path("labwire-pylabrobot.yaml")))
server = InstrumentServer(instrument, confirmation_token="operator-grant")
async with server.serve_websocket("127.0.0.1", 9520):
    ...
```

## Addressing

Everything an operation acts on is named `"<labware>/<item>"`:

```
source_plate        the plate itself
source_plate/A1     one well of it
tips/H12            one tip spot
```

Two things are deliberately **not** accepted, both explained in
[DESIGN.md](DESIGN.md). PyLabRobot's derived names (`source_plate_well_A1`)
resolve internally but leak a naming rule an agent should not have to know, so
they are refused with the canonical address in the error. And PyLabRobot's
range syntax (`plate["A1:H1"]`) is not exposed, because JSON arrays already
carry cardinality and one way to say a thing is better than two:

```json
{"wells": ["source_plate/A1", "source_plate/B1"], "volumes_ul": [50.0, 50.0]}
```

Every failure names what would have worked. An unknown labware lists the deck;
an unknown well reports the grid shape.

## The annotation file

Unlike the ophyd bridge's, this file is **optional**. ophyd carries no units,
so its annotation file exists mostly to supply them; PyLabRobot is consistent
by convention (microlitres, microlitres per second, millimetres), so the
bridge supplies units from a built-in table. What this file is for is the
thing neither library knows, which is what the labware actually holds.

```yaml
version: 1
instrument:
  description: A STARlet running dilutions.
commands:
  transfer: {estimated_duration_s: 4.0}
labware:
  Cor_96_wellplate_360ul_Fb: {description: A Costar 96-well plate.}
resources:
  acid_stock:
    description: 1 M hydrochloric acid.
    hazard: corrosive
    safety_class: S3     # reported and recorded, NOT enforced: see below
    locked: true         # refused outright, which v0.2 can enforce
```

Rules worth knowing:

- **Merging is per field**, from the labware entry (keyed by PyLabRobot class
  or model) to the resource entry (keyed by name), so an override touches only
  what it names.
- **Unknown keys, resources, and commands are errors.** An annotation naming a
  plate that is not on the deck is refused rather than ignored, because a
  silently dropped hazard annotation is the worst failure this file has.
- **`locked` is enforced. `safety_class` here is not.** Locking a plate locks
  all 96 of its wells and refuses every operation touching them. Raising a
  resource to S3 changes what is reported and recorded and nothing else, for
  the reason in LIMITATIONS.

## Mapping

| PyLabRobot | Labwire |
|---|---|
| `LiquidHandler` | one instrument |
| backend class name | `identity.model` |
| deck and its labware | `describe_deck` result, safety class **S0** |
| `Well` / `TipSpot` / `Plate` | an address string in command parameters |
| `aspirate` / `dispense` / `transfer` | commands, safety class **S2** |
| `pick_up_tips` / `drop_tips` / `return_tips` / `discard_tips` | commands, **S2** |
| volume tracker, per well | `describe_deck` contents, listed sparsely |
| tip trackers, per channel | `describe_deck` channels |
| cumulative volume moved | telemetry channels, in `uL` |
| `stop` | command, safety class **S0** |
| `NoTipError` / `HasTipError` | `interlock` |
| `TooLittleLiquidError` / `TooLittleVolumeError` | `validation` |
| `ResourceNotFoundError` / `NoChannelError` | `validation` |
| `ChannelizedError` | first mapped sub-error, all channels in `details` |
| anything else | `hardware_fault` |

Safety defaults lean toward friction. Everything that moves or consumes
material is S2, so an agent must present an operator confirmation for each
call; reads are S0; `stop` is S0 so recovery stays available while an
interlock is tripped. An annotation may raise a class, never lower it.

## LIMITATIONS

Read this before believing anything above.

- **No physical hardware, ever.** The bridge is exercised against
  PyLabRobot's `LiquidHandlerChatterboxBackend`, which prints the operations
  it would have performed. It has never been connected to a Hamilton, a Tecan,
  an Opentrons, or any other machine, and **no claim is made about any vendor
  instrument**. PyLabRobot's simulator backend was removed in favour of a
  websocket Visualizer that opens a browser, so chatterbox is the only honest
  hardware-free option.
- **A hazard annotation is not enforced.** Labwire v0.2 gates S2 and S3
  through the same confirmation stub, so raising a resource to S3 changes what
  is reported and recorded, not what is permitted. `locked` is the only
  escalation this protocol version can actually enforce, which is why it is a
  hard refusal rather than a gradation. See
  [SPEC-FINDINGS.md](../../../SPEC-FINDINGS.md), findings F3 and F4.
- **Enabling tracking is process-wide.** The bridge turns on PyLabRobot's tip
  and volume trackers, because without them a liquid handler silently accepts
  physically impossible commands. PyLabRobot toggles both through module-level
  globals rather than per-handler state, so **constructing one bridged
  instrument changes tracking for every liquid handler in the process.**
- **Volumes are believed, not measured.** The trackers know what they have
  been told and what they have moved. Nothing looks into a plate. A run starts
  with `set_well_volume`, and if that is wrong everything after it is wrong.
- **Cancellation is best-effort and untested on hardware.** Each operation is
  a single await, so a cancel stops the handler and abandons the call rather
  than interrupting it partway. Against the chatterbox backend operations
  complete immediately, so a cancel almost always loses the race. An
  aspiration cannot be un-aspirated in any case (SPEC-FINDINGS F8).
- **Gripper moves are not exposed.** `move_plate`, `move_lid`, and
  `move_resource` move labware through space, where the failure is a collision
  rather than a bad pipetting step. They deserve their own treatment, and
  doing them badly is worse than not doing them.
- **96-head operations are not exposed**, and neither is PyLabRobot's
  untyped `backend_kwargs` passthrough to vendor firmware. Handing an agent an
  untyped channel into a vendor backend would undo the point of a typed
  protocol.
- **Deck state can go stale.** `describe_deck` is a snapshot with no push
  notification and no revision counter, so a second client can move a plate
  and the first will not know (SPEC-FINDINGS F2).

## Licensing

PyLabRobot is a separate MIT-licensed project from the Sculpting Evolution
group at MIT Media Lab. It is an **optional dependency** here: not vendored,
not forked, not modified. See the repository [`NOTICE`](../../../NOTICE).
