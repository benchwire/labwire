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

## What changed in v0.3

The deck stopped being a command and became the `labwire:deck` **resource**:
read it for typed content plus the index of everything a command parameter
can reference. The invented `"plate/A1"` grammar is gone; references are
URIs like `labwire:deck/source_plate/A1`, composed by one protocol-defined
rule, declared with the `resource_ref` keyword instead of a regex, and
validated by the server against a fresh read before a handler runs. And the
**gripper ships**: `move_plate`, `move_lid`, `move_resource` at S3, which
now means an operator grant an agent cannot mint, bound to the exact
parameters of one call.

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
  8 channel(s), 9 piece(s) of labware
    S2  aspirate
    S2  dispense
    S1  set_well_volume
    S3  move_plate
    S0  stop
    labwire:deck/tips: tip_rack 8x12  (96 tips)
    labwire:deck/source_plate: plate 8x12  (hazard: none)
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

Everything an operation acts on is a URI under the deck resource:

```
labwire:deck                       the resource: read it
labwire:deck/source_plate          labware standing on the deck
labwire:deck/source_plate/A1       one well of it
labwire:deck/tips/H12              one tip spot
```

Item URIs compose by the protocol rule (SPEC 10.1): entry URI, slash, an id
from the read result's index, so an agent that can read an index can
construct every legal reference with no grammar to learn. Two things are
deliberately **not** accepted: PyLabRobot's derived names
(`source_plate_well_A1`) are refused with the canonical URI, and its range
syntax (`plate["A1:H1"]`) is not exposed, because JSON arrays already carry
cardinality:

```json
{"wells": ["labwire:deck/source_plate/A1"], "volumes_ul": [50.0]}
```

Every failure names what would have worked, and the server's own refusal
(`-32010`) adds the pointer, the expected kind, did_you_mean candidates,
and a ready-to-send read request.

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
  labwire:deck/acid_stock:
    description: 1 M hydrochloric acid.
    hazard: corrosive
    locked: true         # refused outright; enforced
```

Rules worth knowing:

- **Merging is per field**, from the labware entry (keyed by PyLabRobot class
  or model) to the resource entry (keyed by its deck URI), so an override
  touches only what it names.
- **Unknown keys, resources, and commands are errors.** An annotation naming a
  plate that is not on the deck is refused rather than ignored, because a
  silently dropped hazard annotation is the worst failure this file has.
- **`locked` is enforced.** Locking a plate locks all 96 of its wells and
  refuses every operation touching them.
- **There is no per-resource `safety_class` any more.** It was documented in
  three places as reported-but-not-enforced, and keeping a field that cannot
  raise a call's class would be keeping a lie: argument-dependent classes are
  finding F3, still out of scope. Command-level `safety_class` overrides now
  genuinely bite, because raising a command to S3 makes it require an
  operator grant. `hazard` appears in the deck resource content, where an
  agent actually reads it.

## Mapping

| PyLabRobot | Labwire |
|---|---|
| `LiquidHandler` | one instrument |
| backend class name | `identity.model` |
| deck and its labware | the `labwire:deck` resource (`resource/read`) |
| `Well` / `TipSpot` / `Plate` | a `resource_ref`-typed URI in command parameters |
| `aspirate` / `dispense` / `transfer` | commands, safety class **S2** |
| `pick_up_tips` / `drop_tips` / `return_tips` / `discard_tips` | commands, **S2** |
| `move_plate` / `move_lid` / `move_resource` | commands, **S3**: operator grant, not interruptible |
| volume tracker, per well | deck resource content, listed sparsely |
| tip trackers, per channel | deck resource content |
| cumulative volume moved | telemetry channels, in `uL` |
| `stop` | command, safety class **S0** |
| `NoTipError` / `HasTipError` | `interlock` |
| `TooLittleLiquidError` / `TooLittleVolumeError` | `validation` |
| `ResourceNotFoundError` / `NoChannelError` | `validation` |
| `ChannelizedError` | first mapped sub-error, all channels in `details` |
| anything else | `hardware_fault` |

Safety defaults lean toward friction. Everything that moves or consumes
liquid is S2, so an agent must present an operator confirmation for each
call. Everything that moves **labware through space** is S3: the failure
mode is a collision, and each call takes a single-use operator grant bound
to its exact parameters, which the demo shows being refused, approved,
used, and then refused on different values. `stop` is S0 so recovery stays
available while an interlock is tripped. An annotation may raise a class,
never lower it.

## LIMITATIONS

Read this before believing anything above.

- **No physical hardware, ever.** The bridge is exercised against
  PyLabRobot's `LiquidHandlerChatterboxBackend`, which prints the operations
  it would have performed. It has never been connected to a Hamilton, a Tecan,
  an Opentrons, or any other machine, and **no claim is made about any vendor
  instrument**. PyLabRobot's simulator backend was removed in favour of a
  websocket Visualizer that opens a browser, so chatterbox is the only honest
  hardware-free option.
- **Command-level S3 is enforced; resource-level hazard is not.** Getting
  this distinction right matters more than the feature. An S3 *command*
  requires a real operator grant now (finding F4, resolved). But annotating a
  *resource* as hazardous still cannot raise the class of a call that touches
  it, because safety classes are per command, not per argument: that is
  finding F3, out of scope. `hazard` is surfaced to agents in the deck
  content and `locked` is a hard refusal; neither is a gradation.
- **Gripper destinations are validated against the index, not against
  physics.** A grant authorizes a move the operator saw; nothing here checks
  reachability, collision clearance, or what a real STARlet's rail geometry
  permits. <!-- TODO-VERIFY: which rail range is a legal gripper destination
  on a real STARlet, and whether a decked plate's footprint changes that;
  never tested on hardware. -->
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
