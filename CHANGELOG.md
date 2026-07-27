# Changelog

All notable changes to Labwire. The protocol version (`"0.2"`) and the
package versions move together while the project is pre-1.0; breaking
changes are expected until then, and are called out explicitly.

## Unreleased

### Added

- **`labwire-conformance`** (SPEC §15.3): an executable conformance suite
  that points at ANY server over WebSocket and checks the spec's normative
  requirements: handshake and version negotiation, descriptor validity and
  mandatory units, S2/S3 refusal semantics, resource reads and reference
  validation, error taxonomy, and signed-bundle verification including
  tamper detection. Binary pass/fail per check with spec references; the
  verdict is the §15.1 level actually earned. Command-executing checks are
  opt-in; everything else stops at refusals servers must issue before
  running anything. CI runs it against the reference server.

- **Property-based wire fuzzing** (hypothesis, deterministic in CI):
  arbitrary JSON-RPC envelopes and submit params never kill a session and
  always draw a taxonomy-tagged answer; pathologically deep payloads
  survive; any content-changing byte flip or truncation of a signed bundle
  fails verification (formatting-only flips legitimately still verify,
  since signatures bind RFC 8785 canonical content, not raw bytes), and
  unparseable or non-UTF-8 manifests get a verdict, not a traceback.
  Honest result: the fuzz found no new breaks; the one wire gap of the
  day (below) was found by the conformance suite first.

- **Hardware-ready transports** in `labwire-drivers`: the line-protocol
  link is now pluggable, TCP (as before) or USB-serial via the
  `labwire-drivers[serial]` extra (pyserial-asyncio-fast, BSD-3-Clause,
  the maintained successor of pyserial-asyncio; optional, never
  vendored). Drivers accept a prebuilt `link=`. New: an endpoint file
  format (`load_endpoints`, strict, unknown keys are errors), a
  `labwire probe` command that asks a SCPI endpoint `*IDN?` and drafts
  its annotation file with TODOs for everything a probe cannot know, and
  docs/HARDWARE.md, the walkthrough for the day real equipment arrives.
  Status stated everywhere it matters: real transports, tested against
  simulators (TCP against the sims, serial against a PTY responder),
  awaiting hardware; no vendor compatibility is claimed.

### Fixed

- The WebSocket transport silently dropped unparseable frames instead of
  answering `-32700` (and non-object JSON instead of `-32600`) as SPEC §12
  requires. Found by running the new conformance suite against the
  reference server on its first day.

## 0.3.0, 2026-07-27

Protocol version `"0.3"`: things, not only quantities. Driven by findings
F1, F2, and F4 in [SPEC-FINDINGS.md](SPEC-FINDINGS.md), each now resolved
there with its residual stated.

### Added

- **Resources** (SPEC §7.6, §10): URI-identified, typed, readable instrument
  state, declared in the descriptor beside commands and read with
  `resource/read`. Content schemas carry a scoped `unit` keyword so state is
  as unit-mandatory as commands. Revisions are derived from the canonical
  read result; `resource/changed` rides the event channel under a reserved
  name. The liquid handler's deck is `labwire:deck`; the syringe pump gains
  `labwire:syringe`, a consumable resource on an instrument with no
  references at all, because the primitive is not deck-shaped.
- **Typed references** (SPEC §7.2): the `resource_ref` schema keyword, with
  `kind` matched against a registry (SPEC Appendix A) and `enumerated_by`
  naming the resource whose index lists valid values. Closure is checked
  before a descriptor is served; values resolve against a fresh read at
  submission; the refusal (`-32010`) carries an RFC 6901 pointer, the
  expected kind, the longest resolving prefix, `did_you_mean`, and a
  ready-to-send read request. The SDK's `ResourceRef(...)` builds annotated
  parameter types, so a bridge writes `source: Container` with no regex.
- **Operator grants for S3** (SPEC §8.6): provisioned out of band in a store
  the protocol has no method to write, bound to a command name and the RFC
  8785 digest of its normalized parameters (a binding adopted from LAP with
  credit), expiring and use-limited, consumed atomically. A refused S3
  submission records a pending request; `labwire grant list | approve |
  revoke` is the operator tool; the refusal (`-32011`) says
  `mintable_by_agent: false` in a typed field. A server declaring S3
  commands with no store refuses to start.
- **Optimistic concurrency** (SPEC §10.5): `if_revision` on submit, refused
  with `-32012` before any confirmation or grant is spent; terminal status
  carries `resource_revisions`, so a single agent never re-reads between
  steps.
- **Gripper moves** in `labwire-pylabrobot`: `move_plate`, `move_lid`,
  `move_resource` at S3, non-interruptible, with resource-typed parameters.
  The demos show the ceremony beat by beat, ending with a valid grant
  refused on different parameters. Exercised against the chatterbox backend
  only, **never against physical hardware**.
- The MCP adapter maps resources onto MCP resources, synthesizes a
  model-callable read tool with an enum `uri`, distinguishes S2 confirmation
  from S3 authorization in schemas and descriptions, and serializes error
  details instead of flattening them.

### Breaking

- Protocol version is `"0.3"`; a v0.2 client and a v0.3 server do not
  interoperate.
- `InstrumentDescriptor.resources` is REQUIRED of servers (`[]` allowed).
- **A `confirmation` no longer satisfies `S3`.** Deployments that raised a
  command to S3 stop working until grants are provisioned; the failure is
  loud (`-32011`, reason `absent`), never silent.
- Submission precedence moves `interlock` and capacity ahead of
  confirmation and authorization: everything knowable without an operator
  is checked first (SPEC §12.1). A submit against a tripped interlock now
  returns `-32003` where v0.2 returned `-32009`.
- The error `data` requirement extends to `-32012` (SPEC §12.2).
- **Manifests are `"0.3"`**: `command.params` records the **normalized**
  parameters (v0.2 recorded the raw submission, so a command with defaulted
  optionals signed a manifest describing something other than what ran),
  plus `params_digest`, an `authorization` block with REQUIRED
  `identity_verified: false`, and `resource_revisions`. Verifiers accept
  0.2 and 0.3 bundles both; `labwire verify` refuses a 0.3 bundle claiming
  identity was verified.
- The `unit` and `resource_ref` schema keywords are claimed: `unit`
  REQUIRED on numeric nodes in `content_schema` and forbidden in command
  schemas; `resource_ref` permitted only in `params_schema`, never beside a
  `pattern`.
- `labwire-pylabrobot`: `describe_deck` is deleted (the deck is a
  resource); the `"plate/A1"` address grammar is deleted (references are
  `labwire:deck/...` URIs); the annotation file keys `resources:` by URI
  and loses its per-resource `safety_class`, which was documented three
  times as reported-but-not-enforced.

### Migration

- Instruments with no tree-shaped state: rebuild against 0.3 and change
  nothing; the SDK supplies `resources: []`.
- Instruments that exposed state through a command result: declare a
  `resource(...)` with a content model, move the command's body into its
  `@reader`, and delete the command.
- Deployments using S3: provision a grant store (`grant_store=` or
  `LABWIRE_GRANT_STORE`) and approve requests with `labwire grant`.
- Clients: read `resources` from the descriptor; follow `enumerated_by`
  from any `resource_ref` you cannot fill; treat `-32010`/`-32011`/`-32012`
  per their `details`, which carry the recovery paths.

### Fixed

- Gripper move results in the PyLabRobot bridge reported the doubled origin
  `labwire:deck/deck` for labware standing directly on the deck, a URI that
  does not resolve. The origin is now the deck resource itself. Caught on
  the first live end-to-end run of the agent demo, which also fixed the
  demo's operator-approval harness: the S3 refusal arrives in a turn that
  still ends in `tool_use`, so the pending request id has to be remembered
  across turns or the operator never gets asked.

## 0.2.1, 2026-07-27

Protocol version stays `"0.2"`: no message shape changed.

### Fixed

- **The mandatory-unit guarantee was false for arrays.** SPEC §7.2 said
  "every numeric parameter (JSON Schema type `number` or `integer`)", and the
  implementation read that literally, so a bare `float` parameter without a
  unit was refused at declaration time while `list[float]` was accepted
  silently. Since an eight-channel liquid handler passes every volume as an
  array, the v0.2 headline promise that no quantity crosses the wire without a
  unit did not hold for an entire domain. A parameter or result now needs a
  UCUM code if a number can appear anywhere in a conforming instance:
  through arrays, nested arrays, fixed-length tuples, mappings, `anyOf`,
  `oneOf`, `allOf`, and local `$ref`s, and for the `type`-as-list, `const`,
  and `enum` forms a hand-written descriptor can use. The checker walks
  `patternProperties`, both spellings of `items`, `prefixItems`,
  `additionalItems`, `contains`, `unevaluatedItems`, `unevaluatedProperties`,
  and the `if`/`then`/`else` branches, resolves `$ref` as a general local
  pointer (including draft-07 `#/definitions/`) and follows chains. Enforced
  at declaration time and in wire validation, the same two layers as before.
  Recorded as finding F5 in [SPEC-FINDINGS.md](SPEC-FINDINGS.md), now
  resolved, with the audit that caught the first, insufficient attempt written
  up there in full.
- **Schemas must now be closed.** An open mapping, an untyped value, an array
  that does not declare its `items`, or a reference the build cannot resolve
  is refused rather than assumed to contain nothing, because a schema that
  permits anything permits a quantity. SPEC §7.2 states this normatively and
  the specification's own flagship example, which was open, was fixed.
- A parameter whose type is an **object with numeric fields** is rejected
  rather than served unannotated. `unit_annotations` is keyed by parameter
  name, so one code cannot describe fields of different kinds; the error names
  the paths and says to flatten them.
- **`returns_units` is keyed by path**, so a result that is legitimately a
  tree can be annotated: `labware[].grid.item_max_volume_ul` names a quantity
  three levels down, and a key covers every path beneath it.
- **An open mapping of numbers (`dict[str, float]`) is refused.** It declares
  arbitrarily many quantities of arbitrarily many dimensions and can carry only
  one code, which is the objection the nested-object rule already made;
  withholding the field names must not remove it. Every affected command now
  returns a declared model: the three drivers, the quickstart, and the ophyd
  bridge, whose `read` and `trigger` models are built from the resolved
  channel set so its correctness-by-discipline became correctness by
  construction.
- **Result schemas are generated in serialization mode**, so a pydantic
  `@computed_field`, which reaches the wire but is absent from the validation
  schema, can no longer carry an unannotated quantity.
- **A quantity inside a root-level container is annotated like any other.** A
  path such as `[].volume_ul` was treated as anonymous and satisfied by any
  key, so `list[Reading]` escaped while `Plate{wells: list[Reading]}` was
  refused. Deleting a wrapper model no longer switches the check off.
- **`labwire-ophyd` no longer guesses an ambiguous EPICS unit.** `egu_to_ucum`
  mapped bare `A` to `Ao` (angstrom), so a magnet current PV introspected as a
  length with nothing reported to anyone. `A`, `S`, `G`, `H` and `M` now refuse
  to translate and are reported as unresolved, for the annotation file to
  settle.
- **`labwire-pylabrobot` commands return declared models** rather than
  `dict[str, Any]`. The old opaque return meant `describe_deck` shipped
  `location_mm` in millimetres and `item_max_volume_ul` in microlitres with no
  unit codes, through the wire and into signed manifests. A handler returning
  a model is normalized to plain JSON once in the server, so the wire, the run
  record, and the manifest carry identical bytes.
- Canonicalization (RFC 8785) mishandled numeric types that are not `int` or
  `float`: a float subclass leaked its own `repr` into the signed bytes
  (numpy scalars serialized as `np.float64(0.5)`), and numpy integers raised
  outright. Any instrument publishing numpy values, which every EPICS or
  ophyd device does, could produce a corrupt or unverifiable manifest.

### Added

- **`labwire-pylabrobot`**, a bridge exposing a
  [PyLabRobot](https://github.com/PyLabRobot/pylabrobot) liquid handler as a
  Labwire instrument
  ([packages/bridges/pylabrobot](packages/bridges/pylabrobot)): an address
  grammar (`"plate/A1"`) mapping JSON to PyLabRobot's live resource objects, a
  deck projection that turns 133 KB of raw serialization into under 8 KB an
  agent can plan with, an optional annotation file describing what labware
  holds, and a runtime serving ten operations with every material-moving one
  classified S2. `make demo-pylabrobot` runs a two-fold serial dilution and
  verifies the signed bundle; `make demo-pylabrobot-claude` has a Claude agent
  read the deck and plan the same series. Exercised only against PyLabRobot's
  hardware-free chatterbox backend, **never against physical hardware**; see
  the package's LIMITATIONS section.
- **[SPEC-FINDINGS.md](SPEC-FINDINGS.md)**, an honest account of the eight
  places protocol v0.2 strained against a domain it was not designed for, with
  recommendations for v0.3. Two are blocking: resource references cannot be
  typed the way UCUM units type numbers, and instrument state that is a tree
  has nowhere to live. It also records what did not strain, which is most of
  the protocol.

- **`labwire-ophyd`**, a bridge exposing [ophyd](https://github.com/bluesky/ophyd)
  devices as Labwire instruments
  ([packages/bridges/ophyd](packages/bridges/ophyd)): introspection of a
  device's components, a YAML annotation file supplying the units and safety
  classes ophyd does not carry, a `labwire-ophyd` CLI that generates and
  checks those files, and a runtime that serves a live device over the
  protocol. `make demo-ophyd` scans a simulated beamline rig for a detector
  peak and verifies the signed bundle; `make demo-ophyd-claude` has a Claude
  agent plan the same scan. Verified against `ophyd.sim` devices and a
  caproto soft EPICS IOC over Channel Access, **never against physical
  hardware**; see the package's LIMITATIONS section.

## 0.2.0, 2026-07-26

Physical typing and safety classification, adopted from
[LAP](https://arxiv.org/abs/2606.03755) with credit (see
[PRIOR_ART.md](PRIOR_ART.md)).

### Breaking

- **UCUM unit codes are mandatory.** Every numeric command parameter needs
  an entry in `unit_annotations`, every named numeric result field an entry
  in the new `returns_units`, and every channel a non-empty `unit`: with
  `"1"` for dimensionless quantities. Enforced as a `TypeError` at
  declaration time and as model validation on the wire, so an under-annotated
  descriptor is rejected rather than guessed at. Commands returning unnamed
  numbers (e.g. `dict[str, float]`) must declare `returns_units` explicitly.
- **Protocol version is `"0.2"`.** A v0.1 client and a v0.2 server will not
  agree during `initialize`.
- `manifest_version` is `"0.2"`; signed manifests now include
  `command.safety_class`.

### Added

- **Safety classes `S0`-`S3`** on every command (default `S1`, SPEC §8.6).
  Servers reject `S2`/`S3` submissions without an acceptable `confirmation`
  using the new error `-32009` (`confirmation_required`), checked after
  schema validation and before interlock and capacity checks. `S0` commands
  remain submittable while an interlock is tripped, so recovery is always
  possible. `InstrumentServer(confirmation_token=...)` configures deployment
  policy; `client.submit(..., confirmation=...)` presents it.
- Optional `qudt_quantity_kind` declarations on commands and channels.
- The MCP adapter surfaces parameter and result units plus the safety class
  in tool descriptions, and adds a required `confirmation` input for
  `S2`/`S3` tools.
- The syringe pump's `dispense` is classified `S2` (it consumes reagent);
  the closed-loop demos run under an operator **standing grant** printed in
  their output.
- [PRIOR_ART.md](PRIOR_ART.md) covers LAP, SCP
  ([arXiv:2512.24189](https://arxiv.org/abs/2512.24189)), and MCP tool
  wrapping; the README gained a prior-art and positioning section.
- [ROADMAP.md](ROADMAP.md).

### Known limitations

- `confirmation` proves deployment policy, not operator identity. LAP-style
  cryptographic operator binding is a roadmap item; do not treat v0.2
  confirmation as an audit control (SPEC §14).
- Unit codes are validated for presence, not UCUM grammar.

## 0.1.0, 2026-07-23

Initial release: protocol specification, server and client SDKs
(WebSocket + in-memory transports), three simulated instruments with
native-protocol drivers, ed25519-signed run manifests with
`labwire verify`, an MCP adapter, and a closed-loop optimization demo.
