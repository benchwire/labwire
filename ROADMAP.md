# Roadmap

Candidates for v0.3 and beyond, roughly in the order they would most help a
real self-driving lab. **No dates**: this is a pre-1.0 project and the list
is a statement of intent, not a commitment. Items move only when they land
with tests.

Anything here that Labwire does not do today is stated as missing in
[README.md](README.md), [PRIOR_ART.md](PRIOR_ART.md), or the specification's
conformance table (SPEC §14.2) rather than implied to exist.

## Safety and accountability

- **Cryptographic operator binding for `S2`/`S3` commands.** Today's
  `confirmation` is a deployment token: it proves policy, not identity
  (SPEC §13). The intended successor is an operator token signed over the
  task and the hash of its canonical parameters, as
  [LAP](https://arxiv.org/abs/2606.03755) specifies. This is the single most
  important gap in the current safety story.
- **Reservation leases**: request/renew/release with epochs and
  exclusive-vs-shared-read modes, so two agents cannot interleave on one
  instrument. LAP's design is the reference point.
- Authentication beyond the `api_key` stub, and an authorization model.

## Modeling things, not only quantities

Everything in this section comes from [SPEC-FINDINGS.md](SPEC-FINDINGS.md),
the record of where protocol v0.2 strained while the PyLabRobot bridge was
being built. That document has the reasoning and the failing cases; this is
the work.

- **Typed resource references** (finding F1, blocking). Units made a parameter
  a volume in microlitres; nothing makes a parameter a well that exists on
  this deck. A `reference_annotations` map declaring what kind of thing a
  parameter names, and the command that enumerates the valid values, plus an
  `unknown_reference` error category. Without it every bridge invents its own
  address grammar, which is the fragmentation Labwire exists to end.
- **A state document** (finding F2, blocking). `state/get`, a `state_schema`
  in the descriptor, `notifications/state_changed` with a revision, and a
  `state_revision` on command results so an agent can tell whether the deck it
  planned against is the deck it acted on. Instrument state that is a tree has
  nowhere to live today, so it goes in a command result that nothing marks as
  special.
- **Argument-dependent safety classes** (finding F3). Let a server compute an
  effective class from validated parameters, bounded below by the declared
  class, and report it in the `-32009` error. Dispensing into waste and
  dispensing into a live culture are currently the same command.
- **An `effects` declaration** (finding F6), orthogonal to S0 to S3, so
  operations that change only the instrument's model of the world can be
  described without misusing a scale that grades physical consequence.
- **Preconditions in the descriptor** (finding F7), or at minimum an interlock
  error that names the command clearing it in a structured field, so an agent
  can order a plan correctly on the first attempt instead of discovering it by
  failing.

## Physical typing

- **Unit enforcement that recurses into arrays and nested objects**
  (finding F5). A `float` parameter with no unit is refused at declaration
  time; a `list[float]` is accepted silently, which nearly made the v0.2
  guarantee false for every liquid-handling command. Small fix, and it needs a
  conformance test whose only numeric parameter is an array.
- **Full UCUM grammar validation** (currently only presence is enforced):
  either a maintained library or a vendored grammar, with the UCUM test set.
- **Calibration blocks**: calibration reference and validity window per
  capability, and an uncertainty model on measurement results.
- Typed result models so result units can be enforced field-by-field
  (mapping returns such as `dict[str, float]` name no properties today).

## Ecosystem bridges

- **`labwire-ophyd`: shipped, with caveats.** Any classic ophyd device can
  be served as a Labwire instrument
  ([packages/bridges/ophyd](packages/bridges/ophyd)), verified against
  `ophyd.sim` devices and a caproto soft IOC over Channel Access. Never
  connected to physical hardware; read that package's LIMITATIONS section
  before relying on it.
- **The other direction**: an Ophyd-compatible wrapper so Bluesky *plans*
  can drive Labwire instruments. Only ophyd→Labwire exists today.
- **ophyd bridge follow-ons**: `ophyd-async` support (only classic synchronous
  ophyd is bridged today), array- and enum-valued signals (Labwire v0.2
  channels carry scalars), and progress reporting from `MoveStatus.watch()`
  (ophyd.sim reports no intermediate fractions, so the bridge does not yet
  surface move progress).
- **`labwire-pylabrobot`: shipped, with caveats.** A PyLabRobot liquid
  handler can be served as a Labwire instrument
  ([packages/bridges/pylabrobot](packages/bridges/pylabrobot)), exercised
  against PyLabRobot's hardware-free chatterbox backend only. Never connected
  to a Hamilton, Tecan, Opentrons, or any other machine; read that package's
  LIMITATIONS section before relying on it.
- **PyLabRobot bridge follow-ons**: gripper moves (`move_plate`, `move_lid`,
  `move_resource`, which are the highest-consequence operations and are
  deliberately unexposed), 96-head operations, and a real vendor backend,
  which would be the first path to Labwire driving physical liquid-handling
  hardware.
- **Real-hardware validation against a physical SCPI instrument.** Until
  this happens, Labwire claims no real-hardware compatibility at all.

## Protocol and implementation

- MCP progress relay: forward command progress notifications to MCP clients
  during long tool calls.
- Telemetry channels as MCP resources.
- stdio transport implementation (specified in SPEC §5.2, unimplemented).
- Multi-instrument servers (`instruments/list`): v0.2 is one instrument per
  server.
- Cross-session run persistence and a manifest retrieval method.
- Exhaustive RFC 8785 (JCS) number test vectors before 1.0.

## Governance

- An independent implementation of the specification by someone other than
  this project: the real test of whether the spec is a spec.
- A conformance test suite any implementation can run against itself.
