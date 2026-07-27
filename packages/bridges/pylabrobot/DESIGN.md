# Design: the PyLabRobot bridge

This document is the plan written before the code, kept as the record of why
the bridge is shaped the way it is. Every factual claim about PyLabRobot below
was checked against **PyLabRobot 0.2.1** by running it, not from recollection;
where something is convention rather than a documented guarantee, it says so.

## Why this bridge exists

The [ophyd bridge](../ophyd) proved Labwire can wrap an existing driver
ecosystem. But ophyd devices are *signal-shaped*: a device is a tree of
scalars you read and write, which is exactly the shape Labwire v0.2 was
designed around. Passing that test proved less than it looked like.

Liquid handling is the opposite shape. A liquid handler has almost no
readable scalars. Its commands act on **things**: a well, a tip spot, a plate,
a position on a deck that changes between runs. The interesting state is a
tree, not a signal. If the Labwire capability model only fits signal-shaped
instruments, it is a protocol for detectors and motors wearing the clothes of
a general standard.

So the real deliverable here is not the package. It is
[SPEC-FINDINGS.md](../../../SPEC-FINDINGS.md): an honest list of every place
the protocol strained, written while the strain was happening rather than
recalled afterward.

## What PyLabRobot is

[PyLabRobot](https://github.com/PyLabRobot/pylabrobot) (MIT) is a
hardware-agnostic Python library for liquid handling and adjacent lab
automation, from the Sculpting Evolution group at MIT Media Lab. It backs
Hamilton STAR/Vantage, Tecan EVO, and Opentrons OT-2 machines behind one
frontend, and ships chatterbox backends that need no hardware at all.

Facts that shaped this design, all verified against 0.2.1:

| Fact | Consequence for the bridge |
|---|---|
| Every operation is already `async` | No worker threads. The ophyd bridge needed `asyncio.to_thread` around every call; this one does not. |
| `LiquidHandler` subclasses both `Resource` and `Machine` | The resource tree root is the liquid handler, and the deck is its only child. |
| Operations take live objects (`Sequence[Container]`, `List[TipSpot]`) | Nothing crosses JSON-RPC as an object. The bridge needs an address grammar and a resolver. Hard problem 1. |
| A populated STARlet deck serializes to ~133 KB across 208 resources | The deck cannot be handed to an agent raw. It must be projected. Hard problem 2. |
| Volumes are µL, flow rates µL/s, distances mm, all as bare `float` | Units are consistent by convention but undeclared. A built-in table can supply them with high confidence. |
| Tip and volume tracking default to **off**, toggled by process-wide module globals | The bridge must enable them, and must be honest that the switch is global. Hard problem 4. |
| Errors have **no common base class** | The mapping table is explicit, exception by exception, with a conservative default. |
| Every operation accepts `**backend_kwargs`, untyped, passed to the vendor backend | Deliberately not exposed. See "What is not exposed". |
| `plate["A1"]` returns a `list[Well]`; `plate.get_item("A1")` returns the `Well` | The bridge uses `get_item` internally and never exposes PyLabRobot's string range DSL. |

## The mapping model

```
PyLabRobot                          Labwire
-------------------------------------------------------------------------
LiquidHandler                       one Instrument
  .backend class name               identity.model
  .name                             identity.serial_number
Deck + assigned labware             a read command, not a descriptor field
Well / TipSpot / Plate / TipRack    an address string in command parameters
lh.aspirate / dispense / transfer   commands, safety class S2
lh.pick_up_tips / drop_tips / ...   commands, safety class S2
lh.stop                             command, safety class S0
volume and tip trackers             aggregate telemetry channels + a read command
PyLabRobot exceptions               Labwire typed errors (table below)
```

The instrument surface is deliberately small. PyLabRobot's `LiquidHandler`
has around thirty public coroutines; the bridge exposes nine. The rest are
either 96-head variants of what is already there, gripper moves (see below),
or internals an agent has no business calling.

## Hard problem 1: addressing things that are not scalars

PyLabRobot operations take resource objects. JSON-RPC carries JSON. Something
has to turn `"the A1 well of the source plate"` into a `Well`.

**Decision: a two-part address string, `"<resource>/<item>"`.**

`"source_plate/A1"` names a well; `"tips/A1"` names a tip spot; a bare
`"source_plate"` names the plate itself. The bridge resolves the first part
through `deck.get_resource()` and the second through `get_item()`, and rejects
anything that does not resolve with a `validation` error naming the address.

Three alternatives were considered and rejected:

- **PyLabRobot's own flat names** (`source_plate_well_A1`). They are unique and
  resolvable in one call, but they are derived, so the agent has to know that
  a well of `source_plate` is spelled `source_plate_well_A1` while a tip spot
  of `tips` is spelled `tips_tipspot_A1`. That is an implementation detail
  leaking into the protocol surface.
- **A structured object**, `{"resource": "source_plate", "well": "A1"}`. Honest
  and self-describing, but it makes every well-taking parameter an object
  schema, and lists of them become lists of objects. Verbose to no benefit.
- **PyLabRobot's range DSL** (`plate["A1:H1"]`). Convenient in Python, but it
  is a second cardinality mechanism competing with JSON arrays. An eight
  channel aspirate is `["plate/A1", "plate/B1", ...]`, which JSON Schema can
  already validate for length and type. One way to say a thing.

The finding underneath this decision is larger than the decision. Labwire v0.2
can describe a parameter as a string with a pattern, and that is all. It
cannot say *this string must name a well that currently exists on this deck*.
Units make numbers physically typed; nothing makes references typed. Recorded
in SPEC-FINDINGS as the resource-reference gap.

## Hard problem 2: the deck is state, and the protocol has nowhere to put it

An agent cannot plan a transfer without knowing what is on the deck. Labwire
v0.2 offers three places to put information, and the deck fits none of them:

- **The descriptor** is static capability discovery. The deck changes between
  runs, and a plate can be moved mid-run. A descriptor that claimed otherwise
  would be lying.
- **Telemetry channels** are scalars with UCUM units. A deck is a tree. Even
  flattened, one channel per well across several plates is hundreds of
  declared channels for something that is not a signal.
- **Command parameters and results** are the only remaining option.

**Decision: `describe_deck`, a class S0 command returning a projected tree.**

Projection matters. The raw serialization is 133 KB across 208 resources,
almost all of it geometry an agent will never reason about. The projection
keeps what an agent needs to plan: labware name, kind, model, grid shape, well
capacity, and location; per-channel tip state; and volumes **sparsely**, since
in practice most wells are empty and listing only the non-empty ones keeps the
payload small.

The honest cost of this decision is that deck state is now something an agent
must remember to ask for, and it can go stale the moment another client acts.
Labwire has no way to push it. That is the second finding.

Some liquid-handler state genuinely is signal-shaped, and that part becomes
real telemetry: how many channels currently hold tips, and cumulative volume
aspirated and dispensed. Those are scalars, they carry UCUM units, and they
give a signed run manifest something meaningful to record. Splitting state by
its actual shape rather than forcing all of it one way is the right answer,
and it is worth saying out loud that the protocol only supports half of it.

## Hard problem 3: irreversibility, and why the risk is in the operands

Labwire classifies commands S0 to S3, a taxonomy adopted from LAP. The classes
describe the *command*. In liquid handling the risk lives in the *arguments*.

`dispense` into a waste trough and `dispense` into a live culture are the same
command with the same schema. Aspirating water and aspirating concentrated
acid differ only in which well you name. PyLabRobot cannot help here: its
volume tracker explicitly stopped tracking liquid identity, so the library
does not know what is in the well either.

**Decision: classify conservatively and statically, and record the gap.**

- Anything that moves or consumes material is **S2**: `aspirate`, `dispense`,
  `transfer`, `pick_up_tips`, `drop_tips`, `return_tips`, `discard_tips`.
  An agent presents an operator confirmation for each.
- `stop` is **S0**, so recovery stays available while an interlock is tripped.
- `describe_deck` and the state reads are **S0**.
- `set_well_volume` is **S1**, discussed below.
- The annotation file may **raise** any command to S3. It may never lower one.
  Guessing low is the failure that ruins an experiment.

Two arguments were weighed for making the material-moving operations S3 by
default rather than S2. In favor: a mispipetted reagent can destroy an
irreplaceable sample, and unlike a motor move there is no undo. Against: S3 in
the taxonomy means *hazardous*, and treating every routine pipetting step as
hazardous makes the distinction meaningless, which is how safety gates get
switched off. S2 already requires confirmation; S3 is reserved for the case
the annotation file names explicitly, where the deployer knows the reagent.

The gap this exposes is worth stating precisely, because it is the sharpest
one found. `safety_class` is a static property of a `CommandSpec`. The
protocol already evaluates confirmation *after* parameter validation, so the
hook point for a parameter-dependent decision exists in the message flow; the
spec simply does not allow the answer to depend on the parameters. A v0.3 that
let a server compute an effective safety class from validated arguments, and
report it in the `-32009` error, would close this with a small change to a
place the protocol already goes. Recorded in SPEC-FINDINGS.

### The operation that fits nowhere

`set_well_volume` tells the instrument how much liquid a well contains. It is
how a run starts, since PyLabRobot cannot see into a plate a human placed on
the deck. It moves nothing, so calling it irreversible would be false. It is
also plainly safety relevant: a wrong volume causes an overdraw or aspirating
air on the very next command.

S0 to S3 is a taxonomy of *physical* consequence. An operation that changes
only the instrument's model of the world has no place on it. It is classified
S1 with that reasoning written down rather than forced into a class it does
not belong in, and it is the third finding.

## Hard problem 4: tip and volume state

PyLabRobot's trackers are exactly what an agent-facing bridge needs. They are
transactional, with commit and rollback, and they turn a silent physical
mistake into an exception: aspirating with no tip raises `NoTipError` instead
of moving an empty channel through the motions.

They also default to **off**, and the switch is a module-level global rather
than a property of the liquid handler, so enabling tracking enables it for
every `LiquidHandler` in the process.

**Decision: the bridge enables both trackers when it builds an instrument,
and documents the global bluntly.** Serving without tracking would mean
serving an instrument that silently accepts physically impossible commands,
which is not a defensible thing to hand an agent. The process-wide side effect
goes in LIMITATIONS, not in a footnote.

## Error mapping

PyLabRobot has no common base exception, so the table is explicit and the
default is conservative.

| PyLabRobot | Labwire error | Retryable |
|---|---|---|
| `NoTipError` | `interlock` | no |
| `HasTipError` | `interlock` | no |
| `TooLittleLiquidError` | `validation` | no |
| `TooLittleVolumeError` | `validation` | no |
| `ResourceNotFoundError` | `validation` | no |
| `NoChannelError`, `ChannelsDoNotFitError` | `validation` | no |
| `ChannelizedError` | first mapped sub-error, all reported in `details` | no |
| `RuntimeError` from lifecycle guards | `hardware_fault` | no |
| anything else | `hardware_fault` | no |

`ChannelizedError` is the interesting one: it aggregates per-channel failures
in a dict keyed by channel index. Collapsing it to one message would throw
away which channel failed, so the details carry the whole map.

## Cancellation, honestly

Labwire's `command/cancel` calls `LiquidHandler.stop()`, and the run ends
`canceled`. What that means physically depends entirely on the backend.

The chatterbox backend completes instantly, so cancellation in the demos and
tests is nearly always a race the command wins. This is the same honest
limitation the ophyd bridge documents for simulated axes, and it will be
stated the same way: cancellation is delivered, its physical effect is
device dependent, and it has never been tested against hardware.

An aspiration cannot be un-aspirated in any case. The protocol has no concept
of a point of no return inside a running command, which is a smaller fourth
finding.

## What is not exposed, and why

- **`**backend_kwargs`.** Every PyLabRobot operation accepts an untyped
  keyword passthrough straight to the vendor backend. Handing an agent an
  untyped channel into vendor firmware would undo the point of a typed
  protocol. Not exposed, at any safety class.
- **Gripper moves** (`move_plate`, `move_lid`, `move_resource`). These move
  labware through space, where the failure mode is a collision rather than a
  bad pipetting step. They deserve their own treatment, plausibly S3 with
  reachability checks, and doing them badly is worse than not doing them.
- **96-head operations.** They are variants of what is exposed, and they add
  a second cardinality model for no new insight.
- **`allow_marshal` deserialization.** PyLabRobot can deserialize functions
  via `marshal`; its own docstring calls this a security risk on untrusted
  data. Never enabled.

## Reusing the ophyd annotation format

The format is reused, including the loader, the per-field merge, and the
strict unknown-key rejection. Three things it gets right transfer intact:

- **Per-field merge along the class chain and then the instance**, so an
  override touches only what it names.
- **Unknown keys, components, and commands are errors.** A typo that silently
  annotated nothing is worse than a failure.
- **`exclude` on commands.** Needed more here than there, given how much of
  `LiquidHandler` should stay unexposed.

Two things do not transfer, and one is missing outright:

- **Units are barely needed.** ophyd carries no units at all, so its
  annotation file exists mainly to supply them. PyLabRobot is consistent by
  convention (µL, µL/s, mm), so a built-in table supplies them and the
  annotation file becomes optional. That is a better story, and it means the
  refuse-on-missing-unit behavior almost never fires.
- **`limits` and `dtype` are close to meaningless.** A well's real constraint
  is its `max_volume`, which PyLabRobot already knows, so limit intersection
  has nothing to intersect. Everything is a float.
- **There is no way to annotate an operand.** The ophyd format annotates
  components and commands, which are static structure. The annotation this
  domain actually wants is *the resource named `acid_stock` is hazardous*,
  keyed by a name chosen when the deck was built. That is a third kind of key,
  and the format gains a `resources:` section for it.

## Questions I would have asked

Recorded rather than blocking on, per instruction.

1. Should the bridge expose gripper moves at all in v0.1, or is leaving them
   out the right call given they are the highest-consequence operation?
2. Is S2-with-an-S3-escalation-annotation the right default for pipetting, or
   should reagent-touching operations start at S3 and be lowered explicitly?
3. `SPEC-FINDINGS.md` recommends v0.3 protocol changes. Should those land as
   roadmap entries too, or stay a standalone document until decided?
4. The MIT license needs only the copyright and permission notice; PyLabRobot
   has no NOTICE file. Should NOTICE mention it anyway for symmetry with the
   ophyd entry?
5. Is a serial dilution the most legible demo, or would a plate fill read
   better for someone skimming the repo in thirty seconds?

## Milestones

- **C1** introspection, pure and tested against the chatterbox backend
- **C2** deck projection and the annotation layer
- **C3** the runtime bridge: operations, state, errors, cancellation
- **C4** `make demo-pylabrobot` and `make demo-pylabrobot-claude`
- **C5** SPEC-FINDINGS.md
- **C6** README, repository docs, and a stranger test from a fresh clone
