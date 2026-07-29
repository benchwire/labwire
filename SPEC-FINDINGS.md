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

Eleven findings so far. The two blocking ones (F1, F2) and the enforcement
gap behind the safety story (F4) drove protocol v0.3 and are resolved there,
each with its residual stated in place. F5 was a hole in the v0.2 headline
guarantee, fixed immediately in 0.2.1. F10 arrived from the field and drove
protocol 0.4; F11 came out of carrying 0.4's settlement guarantees across a
foreign protocol boundary. The last section lists what did *not* strain,
which matters just as much and is the part a findings document usually
leaves out.

Findings are kept here after they are fixed, marked resolved with what
changed. This file is a record of what the protocol got wrong, not a task
list; the work itself is scheduled in [ROADMAP.md](ROADMAP.md).

| | Finding | Severity |
|---|---|---|
| F1 | Resource references cannot be typed the way numbers can | **resolved in 0.3** |
| F2 | State that is a tree has nowhere to live | **resolved in 0.3** |
| F3 | Safety class is static when the risk is in the arguments | significant |
| F4 | S2 and S3 are indistinguishable in enforcement | **resolved in 0.3** |
| F5 | Mandatory units do not recurse into arrays | **resolved in 0.2.1** |
| F6 | Operations with no physical consequence have no class | small |
| F7 | Preconditions are discoverable only by failing | small, cheap |
| F8 | A command has no point of no return | worth naming |
| F9 | The unit guarantee covers declarations, not payloads | significant |
| F10 | A stop request returning is not motion stopping | **resolved in 0.4** |
| F11 | Settlement is structured inside Labwire and prose at the boundary | small |

---

## F1. Resource references cannot be typed the way numbers can

**Severity: blocking. RESOLVED in protocol v0.3.** This is the finding that
generalized furthest, and it drove the version.

> **Resolved 2026-07-27.** v0.3 gives references the treatment units got,
> though not in the shape this finding recommended: instead of a sidecar
> `reference_annotations` map, the `resource_ref` keyword rides on the
> parameter's own schema node, following W3C Thing Description practice of
> putting semantics inside the affordance's data schema. The sidecar form
> was tried on paper and rejected because every adapter would have to
> remember to flatten it into prose, and it would hit the same nested-object
> wall `unit_annotations` did. The bridge's invented grammar is deleted;
> URIs compose by one protocol rule from a read result's index; the server
> validates every reference against a fresh read at submission and refuses
> with `-32010`, an RFC 6901 pointer, the expected kind, the longest
> resolving prefix, did_you_mean candidates, and a ready-to-send read
> request. **Residual, stated plainly:** the kind vocabulary (SPEC Appendix
> A) is seeded from one domain and governed by nobody; until a second
> ecosystem adopts it, cross-instrument portability of kinds is a design
> intention, not a demonstrated fact. And validation is
> time-of-check-to-time-of-use; `if_revision` narrows the window and
> nothing closes it.

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

**Severity: blocking. RESOLVED in protocol v0.3.**

> **Resolved 2026-07-27.** Resources are the home: declared in the
> descriptor beside commands so discovery is not skippable, URI-identified,
> read with `resource/read`, revisioned so staleness is detectable, and
> indexed so typed references have something protocol-defined to resolve
> against. The deck is `labwire:deck` now and `describe_deck` is deleted.
> Change notification rides the event channel under a reserved name;
> terminal command status returns the revisions the run changed, so a
> single agent never re-reads between steps; `if_revision` refuses a stale
> plan before any confirmation or grant is spent. The agent demo's prompt
> no longer mentions the deck at all, and CI enforces that the prompt stays
> hint-free and the descriptor leaks no labware names; whether discovery
> alone actually leads a live model to the deck is a claim about model
> behaviour, asserted in the demo rather than in CI, and the README words
> it as designed-for, not verified. **Residual, stated plainly:** no
> pagination (a plate hotel of thousands of positions has no good answer
> yet), no reservation, and no lost-update protection beyond `if_revision`.

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

**Severity: significant. RESOLVED in protocol v0.3.** Known before the
bridge; made concrete by it; fixed by making the classes different
mechanisms rather than different labels.

> **Resolved 2026-07-27.** S2 keeps the session confirmation. S3 takes an
> operator grant an agent structurally cannot produce: provisioned in a
> server-side store the protocol has no method to write, bound to the
> command and the RFC 8785 digest of its normalized parameters (LAP's
> binding, credited), expiring, use-limited, consumed atomically. The
> refusal records a pending request so the operator's approval tool reads
> the real parameters from the server's own store, never from a digest
> relayed through the agent that wants the approval, which closes the
> digest-laundering hole found during design review. The manifest records
> the ceremony with a REQUIRED `identity_verified: false`, and the demo
> shows the beat that matters: a valid, unexpired grant refused on
> different parameters. **Residual, stated plainly:** a grant id is a
> bearer value, `issued_by` is an unauthenticated label, and on one machine
> nothing separates operator from agent but file permissions. v0.3 proves
> deployment policy plus parameter binding plus a bounded window, and still
> not identity; JWS operator tokens remain the successor.

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

**Severity: small, and the cheapest fix here. RESOLVED in 0.2.1.**

> **Resolved 2026-07-27, in three passes.** The first pass covered arrays and
> was wrong to be confident: an adversarial audit of it, run before the claim
> went out, found the guarantee still false in three structural ways and
> demonstrated a live leak in this repository's own PyLabRobot bridge. The
> second pass rewrote the checker, and a second audit of *that* found the
> walker sound but four holes downstream of it. The third pass closed those.
> What changed, and what each audit found, is at the end of this section. The
> finding is kept because findings are history, not a task list.

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

### What was done, in 0.2.1

**The first attempt, and why it was not enough.** The obvious fix was to look
through `items` when collecting numeric parameters. That is what shipped
first, and an adversarial audit run against it immediately afterwards returned
a verdict of *not yet true*. Three structural problems, each reproduced
against a running server:

1. The check was a **keyword allowlist that failed open**. Any JSON Schema
   spelling it did not enumerate reported "no number here": a typeless node,
   `patternProperties`, `unevaluatedItems`, the draft-07 array form of
   `items`, `additionalProperties: true`, a multi-hop `$ref`.
2. The entry points read `schema["properties"]` **without resolving**, so a
   root-level `$ref`, `allOf`, or `if`/`then`/`else` hid the entire property
   set. pydantic emits a bare `$ref` root for any self-referential model.
3. The nested-object check looked exactly **one level deep and never through a
   container**, so `list[Model]` escaped while a bare `Model` was refused.

The third was live in this repository. `describe_deck` in the PyLabRobot
bridge returned `dict[str, Any]`, which pydantic writes as
`{"type": "object", "additionalProperties": true}`, so the checker had nothing
to look at and the command shipped `location_mm` in millimetres and
`item_max_volume_ul` in microlitres with `returns_units: {}`. It then cleared
every downstream layer: accepted on the wire, copied verbatim into the signed
manifest, and rendered to an agent by the MCP adapter with no units line. The
repository's own flagship bridge was falsifying the headline claim in the
first thing a stranger runs.

**The second attempt.** The checker was rewritten as a schema walker that
enumerates every path at which a number can appear and **fails closed**:

- It walks `properties`, `patternProperties`, `items` in both the 2020-12 and
  draft-07 spellings, `prefixItems`, `additionalItems`, `contains`,
  `additionalProperties`, `unevaluatedItems`, `unevaluatedProperties`, and the
  `anyOf`/`oneOf`/`allOf`/`then`/`else` branches, to any depth.
- It resolves `$ref` as a general local RFC 6901 pointer, follows chains,
  handles draft-07 `#/definitions/` as well as `#/$defs/`, merges siblings so
  a stale reference cannot erase a `type: number` next to it, and guards
  cycles with a seen set.
- **A schema that declines to say what it contains is now an error**, not an
  absence. An open mapping, an untyped value, an array with no declared
  `items`, and a reference this build cannot follow are all refused, because a
  schema that permits anything permits a quantity. SPEC §7.2 now requires
  schemas to be closed, and the spec's own flagship example, which was open,
  was fixed.

Results are annotated **by path**, because a result is legitimately a tree:
`returns_units` accepts `labware[].grid.item_max_volume_ul` and a key covers
every path beneath it. Parameters stay flat, and a parameter that is an object
with numeric fields is still refused with instructions to flatten it, since
one code cannot describe fields of different kinds.

**What it cost.** Every PyLabRobot bridge command now returns a declared model
rather than `dict[str, Any]`, which is better for agents anyway, and the
server normalizes a model result to plain JSON once so the wire, the run
record, and the signed manifest carry identical bytes.

Telemetry needed no change, and the reason is recorded as a test rather than a
claim: `ChannelSpec` has always required a non-empty unit for every channel
regardless of dtype, and v0.2 channel dtypes are scalar only.

**The second audit, and the third pass.** The rewritten walker was audited
again. It held: three auditors could not fool it into missing a numeric path,
and its fail-closed handling survived `$anchor`, `$id` re-basing, remote and
unresolvable references, typeless-but-formatted nodes, and depth-bombed
combinators. Every surviving hole was *downstream* of it, which is its own
lesson: a correct analysis can still be discarded by the code that consumes
it.

1. **A container at the root exempted everything inside it.** The coverage
   check asked whether a path named a field, and a path beginning with a
   container marker (`[].volume_ul`) had an empty head, so it was treated as
   anonymous and satisfied by any key at all. `list[WellReading]` was accepted
   with `returns_units={"zzz": "1"}` while the identical
   `Plate{wells: list[WellReading]}` was correctly refused. Deleting a wrapper
   model must not switch the check off.
2. **An open mapping of numbers admitted unlimited quantities under one
   code.** `dict[str, float]` declares arbitrarily many quantities of
   arbitrarily many dimensions, and the checker asked only for one code. The
   named form of the same bundle was already refused with "flatten them";
   withholding the field names removed the objection. This was the repository's
   dominant shape: nine of fourteen shipped driver commands, the quickstart,
   and every generated ophyd command. It is now refused, and every one of them
   returns a declared model.
3. **`@computed_field` reached the wire but not the schema.** The result schema
   was built in pydantic's validation mode, which omits computed fields by
   design, while serialization emits them. A derived quantity (`net = gross
   minus tare`) travelled with no code. The schema is now built in
   serialization mode.
4. **The EGU table guessed.** `egu_to_ucum("A")` returned `"Ao"`, angstrom, for
   what is far more often amperes on a beamline, and because translation
   succeeded no gap was reported to anyone. `A`, `S`, `G`, `H` and `M` are
   genuinely ambiguous and now refuse to translate, so the annotation file has
   to say. This one is unit *correctness* rather than unit presence, and it
   failed silently where a missing unit fails loudly.

**What is still not covered, honestly.** The guarantee is about *declared
schemas*. It does not reach event payloads, progress messages, or the values a
handler actually returns at runtime, none of which are schema-checked against
their declaration. Those are separate gaps, recorded as F9 below rather than
folded into this one.

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

**Severity: worth naming. SUPERSEDED by F10, resolved in protocol 0.4.**
F8 named the problem from the inside; F10 is the same problem arriving
from the field with evidence, and its resolution (declared cancel
semantics, acknowledgment vs settlement, and records that earn their
claims) is the fix this finding said was worth exploring. Kept as
written below, because findings are history.

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

## F9. The unit guarantee covers declarations, not payloads

**Severity: significant.** Found by the audit that verified the F5 fix, and
recorded rather than fixed, because closing it is a design change and not a
patch.

Everything F5 is about happens at declaration and at descriptor validation. A
command's `params_schema` and `returns_schema` are checked, thoroughly now.
Three things are not:

- **Event payloads.** `notifications/event` carries a free-form `data` object.
  An instrument reporting `{"pressure_kpa": 310.0}` in an event is publishing
  a quantity with no unit anywhere in the protocol, and nothing objects.
  Progress messages are the same shape.
- **Signed manifests.** The manifest records the command name, its parameters,
  and its result. It does not record the unit codes that governed them, so a
  verifier reading a bundle in five years gets `{"volume_ul": 50.0}` and has to
  trust the field name. The units were known at signing time and were not
  written down.
- **Runtime result values.** A handler declares `returns_schema` and then
  returns whatever it returns. Nothing validates the actual value against the
  declaration, so a handler can return a field its schema never mentioned.

The pattern is that units are a property of the *contract* and the protocol
has no way to attach them to *data in flight*. That is fine for commands,
where the contract is discoverable ahead of time, and not fine for events,
which have no contract at all.

### Recommendation

Three separate pieces, in descending order of value:

1. **Copy the unit codes into the manifest.** The cheapest and most valuable:
   a signed record should be self-describing, and the codes are already in
   hand when the bundle is written. This alone makes a bundle interpretable
   without the instrument that produced it.
2. **Declare event schemas.** Give an `EventSpec` in the descriptor the same
   treatment `CommandSpec` gets: a payload schema and unit annotations,
   validated by the same walker. Undeclared events stay legal and stay
   unit-free, which is honest about what they are.
3. **Validate results against their declaration** before they leave the
   server, at least in a strict mode. A handler that returns an undeclared
   field is a bug, and the protocol currently ships it.

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
---

## F10. A stop request returning is not motion stopping

**Severity: blocking for any honest safety claim. RESOLVED in protocol
0.4.** The only finding so far that arrived from the field rather than
from building a bridge, which is exactly what a public protocol is for.

Reported by PyLabRobot forum user **vcjdeboer**, who owns an Opentrons
Flex, in the thread on this project's bridge
(<https://discuss.pylabrobot.org/t/bridged-pylabrobot-into-an-agent-facing-protocol/552>),
with the PyLabRobot maintainer concurring on the default backend
behavior. Two facts, both from people with the hardware on the bench:

1. **OT-3/Flex:** `ProtocolEngine.request_stop()` attempts to interrupt
   the running command and cancels queued ones, but by the time it
   returns, things may not have settled and the last command may still
   be running. Only the emergency stop truly halts motion at lower
   layers. A stop RETURNING does not mean motion STOPPED.
2. **Hamilton STAR via PLR:** there is no cancel or abort in the
   backend. `send_command` writes to USB and awaits a future matched by
   a reader thread. Cancelling the coroutine only stops the WAITING;
   the command is already on the wire and the machine executes it
   regardless.

Our bridge did the indicted thing: `_operate` polled for the cancel,
called `lh.stop()` with exceptions suppressed, abandoned the in-flight
await, and reported `canceled` while the hardware, on a real machine,
would have kept moving. The ophyd bridge's own comment said it reported
the cancel "whether or not the device obeys stop()", and `ophyd.sim`
axes' `stop()` is literally `pass`. Reported state diverging from
physical state is the worst failure available to a protocol whose
product is signed records of what physically happened.

### Resolution (protocol 0.4)

- Every command declares `cancel_semantics`: `"abort"` (a real halt
  path the backend can confirm), `"between_steps"` (a bridge-sequenced
  routine that stops only at boundaries), or `"none"` (committed once
  running). Undeclared means `"none"`, and a cancel against `"none"` is
  refused with `-32007`, never accepted-and-ignored.
- Acknowledgment is not settlement: `canceling` means accepted, and
  every run that ends by or during cancellation carries a `cancellation`
  block stating what actually happened: `never_started`, `halted`
  (backend-confirmed only), `halted_at_boundary` (only from a boundary
  checkpoint itself), `ran_to_completion`, or `unconfirmed`, the honest
  case, first-class.
- The block is signed manifest content, and 0.4 manifests record each
  command's declared semantics so `labwire verify` rejects offline what
  the spec forbids: a `canceled` record with no block, a `halted` claim
  from a non-abort command, a boundary claim from a command with no
  boundaries.
- Bridge truth: every atomic PyLabRobot call declares `"none"`;
  `transfer` is bridge-sequenced and stops between steps; the
  abandon-the-await machinery is gone. `EpicsMotor`-family moves
  declare `"abort"`; `ophyd.sim` axes declare `"none"`.
- A 24-agent adversarial review of the implementation confirmed and
  closed nine further settlement holes (blockless shutdown records,
  pre-start cancels claiming halts, boundary claims from mid-step
  abandonment, and others) before release.

### Residual, stated plainly

- `EpicsMotor.stop()` writing the .STOP field has never been exercised
  against a real EPICS IOC (TODO-VERIFY in the bridge).
- An annotation that upgrades a device to `"abort"` asserts that the
  device's status resolution reflects physical reality; for a device
  whose `stop()` resolves its own status locally, `"halted"` is only as
  true as that assertion. The annotation is documented as a truth claim
  about the bench for exactly this reason.
- A `"between_steps"` boundary stops the SEQUENCE; the step in flight
  still runs to completion, and on hardware that step's duration is the
  irreducible latency of any cancel.

## F11. Settlement is structured inside Labwire and prose at the boundary

**Severity: small, found at a foreign protocol boundary.** Porting the
MCP adapter to the 2026-07-28 revision meant expressing 0.4's
cancellation guarantees in another protocol's task model
(`io.modelcontextprotocol/tasks`), and two things happened, one
validating and one instructive.

The validating one: the extension's `tasks/cancel` is cooperative by
design. The acknowledgment promises nothing; the server MAY keep
running. That is F10's resolution arrived at independently by another
protocol team: cancellation as request, not command, with the truth in
what the run finally reports. Labwire's three cancel semantics mapped
onto it without residue: `"none"` acks the cancel and runs to
completion, exactly as the extension permits, and `"abort"` and
`"between_steps"` initiate a real cancel and settle per SPEC 8.3.

The instructive one: the mapping lost information at exactly one place,
and it was the settlement block. A cancelled MCP task carries **no
result field**, so the structured settlement record (outcome, boundary
provenance, the signed bundle reference) has no slot; the adapter
compresses it into a free-text `statusMessage` sentence. Inside Labwire,
settlement is machine-checkable structure; one protocol boundary later
it is prose that each adapter phrases its own way.

**Recommendation.** The spec should define a canonical one-line
settlement summary (a fixed field order rendered as text) so that every
adapter degrades identically when a foreign protocol offers only a
string. Not scheduled for a version yet; recorded here so the next
adapter does not invent a third phrasing.
