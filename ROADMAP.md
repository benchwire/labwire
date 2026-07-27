# Roadmap

Candidates for v0.3 and beyond, roughly in the order they would most help a
real self-driving lab. **No dates**: this is a pre-1.0 project and the list
is a statement of intent, not a commitment. Items move only when they land
with tests.

Anything here that Labwire does not do today is stated as missing in
[README.md](README.md), [PRIOR_ART.md](PRIOR_ART.md), or the specification's
conformance table (SPEC §15.2) rather than implied to exist.

## Safety and accountability

- **Cryptographic operator binding for `S2`/`S3` commands.** Today's
  `confirmation` is a deployment token: it proves policy, not identity
  (SPEC §14). The intended successor is an operator token signed over the
  task and the hash of its canonical parameters, as
  [LAP](https://arxiv.org/abs/2606.03755) specifies. This is the single most
  important gap in the current safety story.
- **Reservation leases**: request/renew/release with epochs and
  exclusive-vs-shared-read modes, so two agents cannot interleave on one
  instrument. LAP's design is the reference point.
- Authentication beyond the `api_key` stub, and an authorization model.

## Modeling things, not only quantities

Protocol v0.3 shipped the heart of this section: **resources** (typed,
URI-identified, readable state, declared in discovery), **typed references**
(the `resource_ref` keyword, validated against current state at submission),
and **operator grants** (S3 authorization an agent cannot mint, bound to the
LAP-credited parameter digest), plus `if_revision` optimistic concurrency.
See [SPEC-FINDINGS.md](SPEC-FINDINGS.md) F1, F2, and F4 for what was built
and the residuals. What remains here is deliberately deferred:

- **Kind registry governance.** SPEC Appendix A is seeded from one domain
  and maintained by one project; a process for admitting kinds, and evidence
  that a second ecosystem can express its references in the same vocabulary,
  are both open.
- **Cryptographic operator identity.** A JWS operator token signed over the
  task and parameter digest, with key distribution and revocation, as LAP
  specifies. v0.3 grants prove deployment policy plus parameter binding plus
  a bounded window; they do not prove who.
- **Resource index pagination.** A 1536-well plate is fine; a plate hotel of
  thousands of positions has no good answer yet.
- **Argument-dependent safety classes** (finding F3), an **effects
  declaration** (F6), and **preconditions in the descriptor** (F7): still
  out of scope, still recorded in SPEC-FINDINGS.

## Physical typing

- **Per-path unit annotation.** 0.2.1 closed finding F5 by requiring a unit
  wherever a number can appear, including inside arrays and mappings. One case
  it could not fix is an object-typed parameter whose fields are quantities of
  different kinds: `unit_annotations` is keyed by parameter name, so such a
  declaration is refused rather than served unannotated. Annotating by path
  would allow it properly.
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
- **PyLabRobot bridge follow-ons**: 96-head operations, and a real vendor
  backend, which would be the first path to Labwire driving physical
  liquid-handling hardware. (Gripper moves shipped in v0.3 as S3
  commands.)
- **Real-hardware validation against a physical SCPI instrument.** The
  transports now exist (TCP and USB-serial, see docs/HARDWARE.md), tested
  against simulators only. Until a physical instrument is on the bench,
  Labwire claims no real-hardware compatibility at all.

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
  this project: the real test of whether the spec is a spec. The
  conformance suite exists for exactly this (`labwire-conformance`,
  SPEC §15.3); what remains is someone using it against code we did not
  write.
