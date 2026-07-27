# Where the protocol fought back

Labwire v0.2 was designed against instruments that are shaped like signals:
you read a scalar, you write a scalar, you stream the scalar over time. Power
supplies, balances, motors, detectors. The [ophyd bridge](packages/bridges/ophyd)
fit that shape, which made it a weaker test than it looked.

A liquid handler does not fit it at all. It has almost no readable scalars.
Its commands act on **things**: a well, a tip spot, a plate that someone put
on the deck this morning. Its interesting state is a tree. Building the
[PyLabRobot bridge](packages/bridges/pylabrobot) was therefore a real test of
whether the capability model generalizes, and this file is the result:
everything that strained, written down while it was straining.

Eight findings. Two are serious enough that a v0.3 which ignored them would be
a protocol for detectors wearing the clothes of a general standard. Three are
small and cheap. The last section lists what did *not* strain, which matters
just as much and is the part a findings document usually leaves out.

| | Finding | Severity |
|---|---|---|
| F1 | Resource references cannot be typed the way numbers can | blocking |
| F2 | State that is a tree has nowhere to live | blocking |
| F3 | Safety class is static when the risk is in the arguments | significant |
| F4 | S2 and S3 are indistinguishable in enforcement | significant |
| F5 | Mandatory units do not recurse into arrays | small, cheap |
| F6 | Operations with no physical consequence have no class | small |
| F7 | Preconditions are discoverable only by failing | small, cheap |
| F8 | A command has no point of no return | worth naming |

---

## F1. Resource references cannot be typed the way numbers can

**Severity: blocking.** This is the finding that generalizes furthest.

Protocol v0.2 made a real advance by requiring UCUM units: a parameter is no
longer "a number", it is a volume in microlitres, and a client that confuses
millilitres for microlitres is caught by the schema rather than by a ruined
plate. That works because a unit is a property of a value.

Liquid handling parameters are mostly not values. They are **references**:

```json
{"wells": ["source_plate/A1", "source_plate/B1"], "volumes_ul": [50.0, 50.0]}
```

`volumes_ul` is fully typed: an array of numbers, in `uL`, declared in
`unit_annotations`. `wells` is `array of string`. The protocol has no way to
say that each string must name a container that exists on this deck right now,
which is the only thing about it that matters.

What the bridge could do, in
[`addressing.py`](packages/bridges/pylabrobot/src/labwire/bridges/pylabrobot/addressing.py),
was invent a grammar (`"<labware>/<item>"`), publish it as a JSON Schema
`pattern`, and check existence at resolution time with an error naming what
would have worked:

```
no labware named 'nonexistent_plate' on the deck; known labware:
dilution_plate, source_plate, tips, trash
```

That is a good error, and it is still a runtime failure standing in for a
type. Worse, the grammar is **this bridge's invention**. The next bridge with
resources will invent a different one, and agents will learn per-bridge string
formats, which is exactly the fragmentation Labwire exists to end.

### Recommendation for v0.3

Give references the treatment units got. Alongside `unit_annotations`, a
`reference_annotations` map declaring what kind of thing a parameter names,
and where the valid values come from:

```json
"reference_annotations": {
  "wells":     {"kind": "container", "enumerated_by": "describe_deck"},
  "tip_spots": {"kind": "tip_site",  "enumerated_by": "describe_deck"}
}
```

`kind` is a small open vocabulary. `enumerated_by` names the command whose
result lists the currently valid values, which is what makes it useful to an
agent rather than merely descriptive: it turns "I do not know what to pass"
into a call it can make. Pair it with a distinct error category
(`unknown_reference`) so a bad reference is not indistinguishable from a bad
number.

This does not require the protocol to model decks, plates, or any domain. It
requires it to admit that some parameters point at instrument state.

---

## F2. State that is a tree has nowhere to live

**Severity: blocking.**

An agent cannot plan a transfer without knowing what is on the deck. Labwire
v0.2 has exactly three places to put information, and the deck fits none:

- **The descriptor** is static capability discovery. A deck changes between
  runs and during them. A descriptor claiming otherwise would be lying.
- **Telemetry channels** are unit-bearing scalars in a time series. A deck is
  a tree. Flattened, one channel per well across several plates is hundreds of
  statically declared channels for something that is not a signal.
- **Command results.** Everything left.

So the deck is a command: `describe_deck`, safety class S0. It works, and the
[projection](packages/bridges/pylabrobot/src/labwire/bridges/pylabrobot/deck.py)
is genuinely useful. PyLabRobot's own serialization of a loaded STARlet deck
is about 133 KB across 208 resources; the projection is under 8 KB because it
keeps what an agent plans with and lists well contents sparsely, since the
empty wells are the ones that can be inferred.

Three things are wrong with it anyway:

1. **Discovery is by convention.** Nothing in the descriptor marks
   `describe_deck` as *the* state read. A client has to be told, which is why
   the agent demo's system prompt opens with "Start by calling describe_deck".
   A protocol that needs a prompt to be usable has moved the specification
   into the prompt.
2. **There is no push.** Another client can move a plate and the first client
   will not know. Telemetry has `notifications/telemetry` for exactly this
   problem, restricted to scalars.
3. **Every bridge invents its own shape.** Same fragmentation as F1.

### Recommendation for v0.3

A first-class state document, which costs little because the machinery already
exists:

- `state/get` returning a server-defined JSON document.
- A `state_schema` field in the descriptor, so the shape is discoverable
  rather than learned.
- `notifications/state_changed`, carrying at minimum a monotonic revision so a
  client knows its copy is stale. A full diff is nicer and not required.
- A `state_revision` on command results, so an agent can tell whether the deck
  it planned against is the deck it acted on. Liquid handling makes the
  lost-update problem concrete: plan a transfer from the projection, have
  another client consume the source well, and the transfer is now wrong in a
  way that produces no error.

This is the single change that would most improve the protocol for anything
that is not a signal, and it does not require modeling any domain.

---

## F3. Safety class is static when the risk is in the arguments

**Severity: significant.**

`safety_class` is a property of a `CommandSpec`. In liquid handling the risk
lives almost entirely in the arguments. `dispense` into a waste trough and
`dispense` into a live culture are the same command with the same schema.
Aspirating water and aspirating concentrated acid differ only in which well
you name.

PyLabRobot cannot help: its volume tracker deliberately stopped tracking
liquid identity, so the library does not know what is in the well either. The
only source of that knowledge is a human, through the annotation file.

So the bridge lets an annotation say a resource is hazardous, and then has
almost nothing to do with the information. It cannot raise that particular
call's class, because the class was fixed when the descriptor was built. What
it can do is
[refuse outright](packages/bridges/pylabrobot/src/labwire/bridges/pylabrobot/bridge.py),
which is why the annotation file has `locked`:

```yaml
resources:
  acid_stock:
    hazard: corrosive
    safety_class: S3   # reported and recorded; not enforced, see F4
    locked: true       # refused outright, which v0.2 *can* enforce
```

A hard refusal is a poor substitute for a gradation, and it is the only
enforcement the protocol left available.

### Recommendation for v0.3

The hook point already exists. SPEC 8.6 orders submission checks as
unsupported, then validation, then confirmation, then interlock, then
capacity. Confirmation is *already* evaluated after the parameters are
validated and available. The spec simply forbids the answer from depending on
them.

Allow a server to compute an **effective safety class** from validated
arguments, bounded below by the declared class so a server can raise but never
lower, and report it:

```json
{"code": -32009, "message": "confirmation required",
 "data": {"category": "confirmation_required",
          "declared_safety_class": "S2",
          "effective_safety_class": "S3",
          "reason": "source_plate is annotated corrosive"}}
```

The declared class stays the honest floor for static reasoning. The manifest
should record the effective class, since that is what actually governed the
run.

---

## F4. S2 and S3 are indistinguishable in enforcement

**Severity: significant.** Known before this bridge; made concrete by it.

v0.2 gates S2 and S3 through the same confirmation stub. A correct
confirmation satisfies both. So raising a command to S3 changes what is
printed and recorded and changes nothing about what is permitted.

This was already documented as a limitation, and writing a bridge where the
distinction genuinely matters shows why it is worse than it sounds. The
annotation file can mark a plate as holding acid, at class S3, and every
enforcement path treats it exactly like buffer. A deployer reading only the
annotation file would reasonably believe otherwise. The bridge's
documentation now says this in three places, which is the sound a protocol
makes when it is missing something.

### Recommendation for v0.3

Make the classes actually differ. The roadmap already carries LAP-style
cryptographic operator binding for S3; until that lands, the minimum honest
version is a **separate credential per class**, so an S3 grant is not
satisfiable by the S2 token that a long-running session leaves lying around.
Standing grants are the normal case for automation, and a standing grant that
silently covers the most dangerous class is the wrong default.

---

## F5. Mandatory units do not recurse into arrays

**Severity: small, and the cheapest fix here.**

Confirmed by running it, not by reading the code:

```python
@command()
async def pour(self, ctx: CommandContext, volume_ul: float) -> dict[str, str]: ...
# TypeError: numeric parameter(s) ['volume_ul'] have no unit annotation

@command()
async def pour(self, ctx: CommandContext, volumes_ul: list[float]) -> dict[str, str]: ...
# accepted, no unit required
```

The check looks at properties whose JSON Schema `type` is `number` or
`integer`. An array of numbers has type `array`, so it passes. Nested objects
presumably have the same hole.

This is not a corner case for this domain. An eight-channel liquid handler
passes *every* volume as an array. The v0.2 headline guarantee, that no
quantity crosses the wire without a unit, was inches from being false for the
entire liquid-handling surface. The bridge annotates its arrays voluntarily;
nothing made it.

### Recommendation for v0.3

Recurse into `items` and into nested `properties` when collecting numeric
parameters, and add a conformance test built from a command whose only numeric
parameter is an array. Small change, and it closes a hole in the guarantee the
version is named for.

---

## F6. Operations with no physical consequence have no class

**Severity: small, but it points at a modeling question.**

`set_well_volume` tells the instrument how much liquid a well already holds.
It is how a run starts, because PyLabRobot cannot see into a plate a human put
on the deck. It moves nothing. It is also plainly safety relevant: a wrong
value causes an overdraw or an aspiration of air on the very next command.

S0 to S3 grades *physical consequence*. An operation that changes only the
instrument's model of the world has no place on that scale. Calling it S2
would be false, since nothing is irreversible. Calling it S0 would suggest it
is always safe. It is classified S1 with the reasoning written down, which is
the best of three imperfect options.

### Recommendation for v0.3

Keep S0 to S3 for physical consequence and add an orthogonal `effects`
declaration: `["material"]`, `["motion"]`, `["instrument_state"]`,
`["none"]`. An agent can then reason about "will this change what I believe"
separately from "can this hurt something", which are genuinely different
questions that the current single axis conflates.

---

## F7. Preconditions are discoverable only by failing

**Severity: small, and cheap to improve.**

`aspirate` requires a tip on the channel. Nothing in the descriptor says so.
An agent finds out by aspirating without one and getting an error, which the
bridge maps to `interlock` because the fix is another operation rather than a
different argument:

```
interlock: Channel 0 does not have a tip.
```

The mapping is right and the error is clear. But an agent planning a series
has to either know the domain or discover it by failing, and the failure is
S2, so discovery costs a confirmed command.

### Recommendation for v0.3

At minimum a convention: an `interlock` error names the command that clears
it, in a structured field rather than in prose. Better, an optional
`preconditions` list on a `CommandSpec`, naming interlocks that must be clear
or commands that must have run. Even a non-normative "this is usually
preceded by" would let an agent order a plan correctly on the first attempt.

---

## F8. A command has no point of no return

**Severity: worth naming; no clean fix proposed.**

Labwire models cancellation as available for the whole life of a running
command. An aspiration is not like that. Partway through, the liquid is in the
tip, and there is no state to return to: cancelling does not put it back.

The bridge's cancellation is honest about what it can do. It stops the handler
and abandons the call rather than interrupting it partway, and the
[LIMITATIONS section](packages/bridges/pylabrobot/README.md) says that against
a simulated backend the command almost always wins the race, and that hardware
behaviour has never been tested. But `command/cancel` succeeding tells a
client the operation was undone, and for liquid handling that is not what
happened.

### Recommendation for v0.3

Not a resolved design, deliberately. The direction worth exploring is letting
a command report a phase transition after which cancellation is refused with
`NotCancelable`, so an agent can distinguish "stopped before anything
happened" from "stopped, and the reagent is gone". The progress mechanism
already carries per-run updates and could carry this.

---

## What did not strain

This is the part that would be dishonest to leave out. Most of the protocol
handled a domain it was not designed for without complaint.

- **Units on scalar quantities.** UCUM codes carried `uL`, `uL/s`, and `mm`
  with no friction. The domain's conventions map cleanly, and requiring them
  at declaration time caught real omissions while the code was being written.
  F5 is a hole in the enforcement, not a flaw in the idea.
- **The command lifecycle.** Submit, accept, run, terminal state, with results
  and typed errors, fit liquid handling exactly. Nothing had to be worked
  around.
- **The error taxonomy.** PyLabRobot has no common base exception and a
  scattered set of error types, and every one of them landed somewhere
  sensible. The interesting case, `ChannelizedError`, aggregates per-channel
  failures, and the `details` field carried the whole map without extension.
- **Safety confirmation, mechanically.** Gating every material-moving command
  behind an operator grant worked precisely as intended, and a standing grant
  is the right shape for a dilution series issuing dozens of them. The
  problems are F3 and F4, both about *which* class applies, not about the
  mechanism.
- **Signed manifests.** A transfer produces a bundle that verifies, recording
  the command, its parameters including the volumes, and its safety class. No
  changes were needed for a domain with no telemetry to speak of.
- **Capability discovery for commands.** JSON Schema described every operation
  including array cardinality, which is why this bridge does not expose
  PyLabRobot's `"A1:H1"` range syntax: JSON arrays already carry cardinality,
  and one way to say a thing is better than two.
- **`max_concurrent_commands`.** A liquid handler has one arm, and declaring
  one slot made overlapping submissions fail correctly with no extra work.

The pattern in the findings is consistent and worth stating plainly. Labwire
v0.2 models **actions on quantities** very well. It does not yet model
**things**. Units gave values a type; F1 asks for the same for references. The
descriptor says what an instrument can do; F2 asks it to also say what exists
to do it to. Safety classes grade commands; F3 asks them to grade calls.

Those three changes would take the protocol from one that fits signal-shaped
instruments to one that fits laboratories.
