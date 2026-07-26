# Roadmap

Candidates for v0.3 and beyond, roughly in the order they would most help a
real self-driving lab. **No dates** — this is a pre-1.0 project and the list
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
- **Reservation leases** — request/renew/release with epochs and
  exclusive-vs-shared-read modes, so two agents cannot interleave on one
  instrument. LAP's design is the reference point.
- Authentication beyond the `api_key` stub, and an authorization model.

## Physical typing

- **Full UCUM grammar validation** (currently only presence is enforced):
  either a maintained library or a vendored grammar, with the UCUM test set.
- **Calibration blocks** — calibration reference and validity window per
  capability, and an uncertainty model on measurement results.
- Typed result models so result units can be enforced field-by-field
  (mapping returns such as `dict[str, float]` name no properties today).

## Ecosystem bridges

- **`labwire-bluesky`** — an Ophyd-compatible device wrapper so Bluesky
  plans can drive Labwire instruments, and Labwire can expose Ophyd devices.
- **`labwire-pylabrobot`** — a bridge to PyLabRobot backends, which would
  also be the first path to Labwire driving real liquid-handling hardware.
- **Real-hardware validation against a physical SCPI instrument.** Until
  this happens, Labwire claims no real-hardware compatibility at all.

## Protocol and implementation

- MCP progress relay: forward command progress notifications to MCP clients
  during long tool calls.
- Telemetry channels as MCP resources.
- stdio transport implementation (specified in SPEC §5.2, unimplemented).
- Multi-instrument servers (`instruments/list`) — v0.2 is one instrument per
  server.
- Cross-session run persistence and a manifest retrieval method.
- Exhaustive RFC 8785 (JCS) number test vectors before 1.0.

## Governance

- An independent implementation of the specification by someone other than
  this project — the real test of whether the spec is a spec.
- A conformance test suite any implementation can run against itself.
