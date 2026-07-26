# Changelog

All notable changes to Labwire. The protocol version (`"0.2"`) and the
package versions move together while the project is pre-1.0; breaking
changes are expected until then, and are called out explicitly.

## 0.2.0 — 2026-07-26

Physical typing and safety classification, adopted from
[LAP](https://arxiv.org/abs/2606.03755) with credit (see
[PRIOR_ART.md](PRIOR_ART.md)).

### Breaking

- **UCUM unit codes are mandatory.** Every numeric command parameter needs
  an entry in `unit_annotations`, every named numeric result field an entry
  in the new `returns_units`, and every channel a non-empty `unit` — with
  `"1"` for dimensionless quantities. Enforced as a `TypeError` at
  declaration time and as model validation on the wire, so an under-annotated
  descriptor is rejected rather than guessed at. Commands returning unnamed
  numbers (e.g. `dict[str, float]`) must declare `returns_units` explicitly.
- **Protocol version is `"0.2"`.** A v0.1 client and a v0.2 server will not
  agree during `initialize`.
- `manifest_version` is `"0.2"`; signed manifests now include
  `command.safety_class`.

### Added

- **Safety classes `S0`–`S3`** on every command (default `S1`, SPEC §8.6).
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

## 0.1.0 — 2026-07-23

Initial release: protocol specification, server and client SDKs
(WebSocket + in-memory transports), three simulated instruments with
native-protocol drivers, ed25519-signed run manifests with
`labwire verify`, an MCP adapter, and a closed-loop optimization demo.
