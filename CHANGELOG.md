# Changelog

All notable changes to Labwire. The protocol version (`"0.2"`) and the
package versions move together while the project is pre-1.0; breaking
changes are expected until then, and are called out explicitly.

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
  confirmation as an audit control (SPEC §13).
- Unit codes are validated for presence, not UCUM grammar.

## 0.1.0, 2026-07-23

Initial release: protocol specification, server and client SDKs
(WebSocket + in-memory transports), three simulated instruments with
native-protocol drivers, ed25519-signed run manifests with
`labwire verify`, an MCP adapter, and a closed-loop optimization demo.
