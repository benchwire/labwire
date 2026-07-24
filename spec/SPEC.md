# Labwire Protocol Specification

**Version:** 0.1.0 (Draft)
**Protocol version string:** `"0.1"`
**Date:** 2026-07-23
**License:** Apache-2.0

---

## 1. Abstract & Status of This Document

Labwire is an open protocol for AI-controlled laboratory instruments. It gives
agents — human-operated software and autonomous AI systems alike — a universal
way to **discover** an instrument's capabilities, **command** it, **stream**
its measurements, and receive **cryptographically signed** records of what was
done. The protocol is JSON-RPC 2.0 over WebSocket or stdio, with a capability
discovery model inspired by the Model Context Protocol (MCP).

This document is a **draft**. Version 0.1 is developed alongside a reference
implementation built in milestones; §14 states exactly which parts of this
specification the reference implementation realizes at any given time. Breaking changes are expected before
1.0.

## 2. Terminology & Conformance

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**,
**SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this
document are to be interpreted as described in BCP 14 [RFC 2119] [RFC 8174]
when, and only when, they appear in all capitals, as shown here.

- **Instrument Server (server):** a process that exposes exactly one
  instrument — real or simulated — over this protocol.
- **Agent Client (client):** a process that connects to an Instrument Server
  to discover, command, and observe the instrument. Typically an AI agent, an
  orchestrator, or a human-facing tool.
- **Session:** one transport connection between a client and a server,
  beginning when the transport opens and ending when it closes; made
  operational by initialization (§6).
- **Command:** a named operation the instrument can perform, declared in the
  instrument's descriptor (§7) and executed via the command lifecycle (§8).
- **Run:** a single execution of a command, identified by a `command_id`.
- **Channel:** a typed, named stream of measurements (§7, §9).
- **Event:** a discrete occurrence reported by the server (§10).
- **Interlock:** a declared safety condition which, while tripped, prevents
  command execution (§7, §8, §10).
- **Run manifest:** a signed record of a completed run (§12).

All JSON field names defined by this protocol use `snake_case`. Unless
otherwise stated, unrecognized fields MUST be ignored by both parties
(forward compatibility).

## 3. Protocol Overview

Labwire uses JSON-RPC 2.0 [JSONRPC] messages over a bidirectional transport.
The client issues **requests**; the server answers with **responses** and
pushes **notifications** (command progress, telemetry, events). In v0.1 the
server MUST NOT issue requests to the client, with one exception: `ping`
(§6.3), which either party MAY send and the receiver MUST answer. The only
client-to-server notification is `notifications/initialized`.

```
Agent Client                                   Instrument Server
     │                                                 │
     │  initialize ───────────────────────────────────▶│
     │◀─────────────────────────────── initialize result
     │  notifications/initialized ────────────────────▶│
     │                                                 │
     │  instrument/describe ──────────────────────────▶│
     │◀──────────────── result: InstrumentDescriptor   │
     │                                                 │
     │  command/submit {command, params} ─────────────▶│
     │◀──────────────── result: {command_id, accepted} │
     │◀─────────── notifications/command_status (push) │
     │                                                 │
     │  telemetry/subscribe {channels} ───────────────▶│
     │◀──────────────── result: {subscription_id}      │
     │◀─────────────── notifications/telemetry (push)  │
     │◀─────────────── notifications/event (push)      │
     │                                                 │
```

A typical agent session: initialize → describe → submit a command → watch
pushed status until a terminal state → read telemetry → repeat → disconnect.

### 3.1 JSON-RPC usage

- Messages MUST conform to JSON-RPC 2.0. The `jsonrpc` member MUST be `"2.0"`.
- Request `id`s MUST be JSON integers, and MUST NOT be reused within a
  session by the same sender.
- JSON-RPC **batch arrays MUST NOT be used.**
- A party receiving a request for a method it does not implement MUST respond
  with JSON-RPC error `-32601` (method not found).

## 4. Versioning & Negotiation

The protocol version is a string of the form `"MAJOR.MINOR"`. This document
specifies protocol version `"0.1"`.

- The client states its protocol version in `initialize`.
- The server replies with the protocol version **it will speak** — the highest
  version it supports that is compatible with the client's request.
- If the client does not support the version in the server's reply, the client
  MUST close the connection.
- Servers SHOULD accept any client version that shares their MAJOR version.
  For MAJOR version 0, the MINOR version carries compatibility significance:
  servers SHOULD reply with exactly `"0.1"` if they implement this document.

The specification document itself is versioned `MAJOR.MINOR.PATCH`
(this document: 0.1.0); PATCH revisions never change the wire protocol.

## 5. Transports

Labwire defines two transports. A conforming server MUST implement at least
one. All transports carry UTF-8 encoded JSON-RPC messages; framing is the
transport's job, and exactly one JSON-RPC message occupies one frame.

### 5.1 WebSocket

- Each JSON-RPC message MUST be sent as one WebSocket **text** frame
  containing one JSON object.
- Binary frames are reserved for future use (bulk data). In v0.1 a receiver
  MUST ignore binary frames.
- WebSocket protocol-level ping/pong frames MAY be used for keepalive by
  either party.
- Servers MAY serve plaintext `ws://` on loopback or isolated lab networks;
  deployments crossing any network boundary SHOULD use `wss://` (see §13).
- Port 9520 is the RECOMMENDED default port. This is a convention, not a
  requirement. <!-- TODO-VERIFY: 9520 unassigned in the IANA Service Name and
  Transport Protocol Port Number Registry -->


### 5.2 stdio

- The server communicates over its standard input/output streams: the client
  writes to the server's stdin, the server writes to the client's stdout.
- Messages are newline-delimited JSON: one JSON-RPC message per line,
  terminated by `\n`. A message MUST NOT contain unescaped embedded newlines.
- The server MUST NOT write anything to stdout that is not a protocol
  message. Logging MUST go to stderr.
- The JSON-RPC `ping` request (§6.3) provides liveness where WebSocket
  ping/pong is unavailable.

## 6. Session Lifecycle

### 6.1 Initialization

The first message in a session MUST be an `initialize` request from the
client. **Initialization completes when the server receives
`notifications/initialized`.** Requests other than `ping` received before
that point MUST be rejected with error `-32002` (`busy`), with
`retryable: false` (§11.1). An `initialize` request received after
initialization has completed MUST be rejected with `-32600` (invalid
request).

`initialize` params:

- `protocol_version` (string, REQUIRED) — the client's protocol version.
- `client_info` (object, REQUIRED) — `{name, version}` identifying the client
  software.
- `capabilities` (object, REQUIRED) — reserved for client capability flags;
  MAY be empty in v0.1.
- `api_key` (string, OPTIONAL) — see §13.

`initialize` result:

- `protocol_version` (string, REQUIRED) — the version the server will speak
  (§4).
- `server_info` (object, REQUIRED) — `{name, version}` identifying the server
  software.
- `capabilities` (object, REQUIRED) — server capability flags. Defined in
  v0.1: `telemetry` (boolean), `events` (boolean), and `manifests` (boolean —
  the server produces signed run manifests, §12). Absent flags default to
  `false`. A request for a method belonging to a capability the server
  advertised as `false` MUST be rejected with `-32001` (`unsupported`).

After receiving the result, the client MUST send the
`notifications/initialized` notification before any other message. The
session is then **operational**.

### 6.2 Shutdown

There is no shutdown method. Either party ends the session by closing the
transport. On close, the server MUST treat all of the session's telemetry
subscriptions as cancelled. Commands already accepted continue to execute
(instruments are physical processes); their terminal states are simply no
longer observable in this session — recoverable via `command/status` in a
future session only if the server persists run state, which v0.1 does not
require.

### 6.3 Liveness

The `ping` request (empty params object) MUST be answered with an empty
result object as soon as it is received, at any point in the session —
including before initialization. Either party MAY send `ping` to detect a
stalled peer; it is the sole request a server may issue (§3).

### 6.4 Concurrent sessions

A server MAY accept multiple simultaneous sessions. Session-scoped rules:

- `notifications/command_status` for a run is delivered only to the session
  that submitted it. `command/status` polling MUST work from any session
  that presents the `command_id`.
- Telemetry notifications are delivered only to the subscribing session.
- Events (§10) are delivered to every operational session.
- `max_concurrent_commands` (§8.4) is a per-instrument limit shared across
  all sessions.

## 7. Instrument Discovery & Capability Description

The `instrument/describe` request (empty params) returns the
**InstrumentDescriptor**: everything a client needs to operate the instrument
without out-of-band knowledge.

### 7.1 InstrumentDescriptor

- `identity` (object, REQUIRED):
  - `manufacturer` (string, REQUIRED)
  - `model` (string, REQUIRED)
  - `serial_number` (string, REQUIRED)
  - `firmware_version` (string, REQUIRED)
  - `firmware_hash` (string, OPTIONAL) — `"sha256:<hex>"` of the firmware
    image, when known. Simulated instruments SHOULD hash their implementing
    code's version identity instead.

  This identity object is embedded verbatim in run manifests (§12).
- `commands` (array, REQUIRED) — see §7.2.
- `channels` (array, REQUIRED) — see §7.3.
- `interlocks` (array, REQUIRED) — see §7.4.
- `max_concurrent_commands` (integer, OPTIONAL, default `1`) — how many
  commands the instrument executes simultaneously (§8.4).

### 7.2 Command declaration

Each entry in `commands`:

- `name` (string, REQUIRED) — unique within the instrument. Names beginning
  with `x-` are reserved for vendor extensions and MUST take the form
  `x-<vendor>/<name>` (§7.5); all other names are instrument-defined.
- `title` (string, REQUIRED) — short human/agent-readable label.
- `description` (string, REQUIRED) — what the command does, in enough detail
  for an agent to decide when to use it.
- `params_schema` (object, REQUIRED) — a JSON Schema (draft 2020-12) object
  describing the command's `params`. Note that the empty schema `{}`
  constrains nothing; commands that take no parameters SHOULD declare
  `{"type": "object", "additionalProperties": false}`.
- `unit_annotations` (object, OPTIONAL) — maps parameter name → unit string.
  Units SHOULD be UCUM case-sensitive codes (e.g. `"mL/min"`, `"Cel"`,
  `"g"`, `"V"`). In v0.1, unit strings are opaque to the protocol: they are
  documentation for the agent, not validated wire syntax.
- `returns_schema` (object, OPTIONAL) — JSON Schema for the command's
  `result` value.
- `estimated_duration_s` (number, OPTIONAL) — typical wall-clock duration.
  Clients SHOULD use it to choose timeouts, and SHOULD apply their own
  default timeout when it is absent.
- `interruptible` (boolean, REQUIRED) — whether `command/cancel` can
  interrupt this command mid-run (§8.3).
- `clears_interlocks` (array of strings, OPTIONAL) — declared interlock
  names this command can clear. See §8.5: such a command remains submittable
  while a named interlock is tripped.

Command results MUST be JSON-serializable values. This is what allows a
Labwire command to be exposed as a tool by agent frameworks with at most a
name mapping (e.g. MCP tools, where `params_schema` maps directly to a
tool's input schema; tool-name character sets may require renaming
<!-- TODO-VERIFY: allowed tool-name characters in the current MCP
revision -->).

### 7.3 Channel declaration

Each entry in `channels`:

- `name` (string, REQUIRED) — unique within the instrument.
- `description` (string, REQUIRED).
- `dtype` (string, REQUIRED) — one of `"float64"`, `"int64"`, `"bool"`,
  `"string"`. Values of `int64` channels MUST fit the IEEE-754
  exactly-representable integer range (|v| ≤ 2^53 − 1) in v0.1. Non-finite
  `float64` values (NaN, ±Infinity) MUST NOT be sent as samples; servers
  MUST either suppress such samples or report the condition as an event.
- `unit` (string, REQUIRED) — unit of the channel's values; SHOULD be UCUM
  (opaque in v0.1, as above). Use `"1"` (UCUM unity) for dimensionless
  channels.
- `sample_rate_hz_hint` (number, OPTIONAL) — the natural production rate.

### 7.4 Interlock declaration

Each entry in `interlocks`:

- `name` (string, REQUIRED) — unique within the instrument.
- `description` (string, REQUIRED) — the condition it protects against and
  how it clears.
- `kind` (string, REQUIRED) — `"hard"` (trips autonomously in the
  instrument; cannot be cleared over the protocol) or `"soft"` (may be
  clearable by an instrument-defined command — see `clears_interlocks`,
  §7.2).
- `tripped` (boolean, REQUIRED) — whether the interlock is tripped at the
  time the `instrument/describe` response is produced. Consumers MUST treat
  this as a snapshot, kept current only via the `interlock/tripped` and
  `interlock/cleared` events (§10).

Interlock behavior is specified in §8.5 and §10.

### 7.5 Vendor extensions

Command names and event names beginning with `x-<vendor>/` (e.g.
`x-sim/inject_fault`) are vendor extensions; the `x-` prefix is reserved
exclusively for this form. Servers MAY expose them; clients MUST NOT assume
their presence. Extension commands MUST still be declared in `commands` with
full schemas.

## 8. Command Lifecycle

### 8.1 States

A run moves through these states:

```
         ┌──────────┬────────────► succeeded
         │          │
accepted ──► running ──────────► failed
   │     │          │              ▲
   │     └──────────┴► canceling ──┤
   │                        │      │
   └────────────────────────┼──────┘
                            └────► canceled
```

- `accepted` — validated and queued for immediate execution.
- `running` — executing.
- `canceling` — cancellation initiated; outcome not yet determined.
- `succeeded`, `failed`, `canceled` — **terminal** states. A run in a
  terminal state MUST NOT change state again.

Legal transitions: `accepted → running | canceling | failed`;
`running → succeeded | failed | canceling`;
`canceling → canceled | succeeded | failed`. The direct edges out of
`accepted` cover cancellation before start (§8.3) and interlock abort
(§8.5).

### 8.2 Submit and status

`command/submit` params: `command` (string, REQUIRED — a declared command
name), `params` (object, REQUIRED — validated against the command's
`params_schema`; MAY be `{}`).

An undeclared `command` name MUST be rejected with `-32001` (`unsupported`).
If `params` violate the command's `params_schema`, the server MUST reject
the request with `-32000` (`validation`). In both cases the server MUST NOT
create a run. Otherwise the server assigns a `command_id` (string, unique
per instrument, RECOMMENDED: UUIDv4) and responds
`{command_id, status: "accepted"}`.

**Status is push-first.** On every state transition out of `accepted`, the
server MUST send `notifications/command_status` to the submitting session
(§6.4). The server MUST write the `command/submit` response before any
`notifications/command_status` for that run; the transition *into*
`accepted` is reported only by the submit response. A run's terminal
`notifications/command_status` MUST be sent after all telemetry and events
attributed to that run. The notification carries the **CommandStatus**
object:

- `command_id` (string, REQUIRED)
- `status` (string, REQUIRED) — a state from §8.1.
- `progress` (object, OPTIONAL) — `{fraction?, message?}`; `fraction` is
  0.0–1.0. Servers MAY additionally send `running` notifications that change
  only `progress`.
- `result` (any, OPTIONAL) — present iff `status` is `succeeded`; conforms to
  the command's `returns_schema` if declared.
- `error` (object, OPTIONAL) — an Error object (§11.2); present iff
  `status` is `failed`, except that servers MAY additionally attach an
  error with category `canceled` (code `-32006`) to `canceled` runs.

`command/status` params `{command_id}` MUST return the current
CommandStatus; unknown `command_id` → error `-32000` (`validation`).
Polling MUST work even though push is required, so that simple clients need
no notification handling. To keep that guarantee meaningful, the server
MUST retain a run's terminal CommandStatus at least until the session that
submitted it closes; cross-session persistence is not required (§6.2).

### 8.3 Cancellation

`command/cancel` params: `{command_id}`.

- For a cancelable run (`accepted`, or `running` with `interruptible: true`),
  the server MUST initiate cancellation and reply with the current
  CommandStatus (typically `canceling`). The terminal state — `canceled`, or
  `succeeded`/`failed` if completion won the race — arrives via
  `notifications/command_status`.
- For a run that is already terminal, in state `canceling`, or `running`
  but not interruptible, the server MUST reply with error `-32007`
  (`not_cancelable`). An unknown `command_id` → error `-32000`
  (`validation`), as in `command/status`. Cancellation is therefore
  idempotent-safe: a duplicate cancel fails cleanly without affecting the
  run, and "no such run" remains distinguishable from "cannot cancel".

### 8.4 Concurrency

An instrument executes at most `max_concurrent_commands` (§7.1) runs
simultaneously. While at capacity, the server MUST reject `command/submit`
with error `-32002` (`busy`). The protocol defines **no server-side
queueing**: queueing, retry, and scheduling are client (agent) policy. The
capacity `busy` error is retryable (§11.2); servers MAY include
`details.retry_after_s` (number) as a backoff hint.

### 8.5 Interlocks

While any declared interlock is **tripped** (§10):

- New `command/submit` requests MUST be rejected with error `-32003`
  (`interlock`) — except that a command whose `clears_interlocks` (§7.2)
  names a currently tripped interlock MUST be accepted. This is what makes
  soft-interlock recovery possible over the protocol.
- Runs in `accepted`, `running`, or `canceling` MUST transition to `failed`
  with an error of category `interlock`, unless the instrument can safely
  complete them (instrument-defined; completing is the exception, failing is
  the rule).

The server MUST emit `interlock/tripped` and `interlock/cleared` events
(§10). How an interlock clears is instrument-defined and MUST be stated in
its `description` (e.g. a hard interlock clears only at the instrument;
a soft interlock clears via a declared command).

## 9. Streaming Telemetry

### 9.1 Subscribe / unsubscribe

`telemetry/subscribe` params:

- `channels` (array of strings, REQUIRED) — declared channel names. An
  undeclared name → error `-32000` (`validation`), and no subscription is
  created.
- `max_rate_hz` (number, OPTIONAL) — per-channel ceiling on delivery rate.
  Servers SHOULD honor it by dropping intermediate samples.

Result: `{subscription_id}` (string, unique per session). Servers MUST
support multiple concurrent subscriptions per session, including
overlapping channel sets. A server whose `telemetry` capability is `false`
MUST reject `telemetry/subscribe` with `-32001` (`unsupported`) (§6.1).

`telemetry/unsubscribe` params `{subscription_id}` → empty result `{}`.
Unknown `subscription_id` → error `-32000` (`validation`). Subscriptions end
at unsubscribe or transport close.

### 9.2 Data notifications

For each sample delivered to a subscription, the server sends
`notifications/telemetry`:

- `subscription_id` (string, REQUIRED)
- `channel` (string, REQUIRED)
- `seq` (integer, REQUIRED) — per-channel sample sequence number assigned
  at *production*, independent of subscriptions: it MUST increment by
  exactly 1 for each successive sample the channel produces, so a jump in
  received `seq` indicates dropped samples. Two subscribers of the same
  channel observe the same `seq` for the same physical sample. `seq` is
  monotonic within one server process lifetime; clients MUST treat a
  decrease as a server restart, not a gap.
- `timestamp` (string, REQUIRED) — RFC 3339 UTC with fractional seconds,
  e.g. `"2026-07-23T15:30:00.123456Z"`. This is the measurement time as the
  instrument knows it.
- `value` (REQUIRED) — JSON value matching the channel's declared `dtype`.

### 9.3 Delivery semantics

Delivery is **best-effort**: under load or rate limits, servers MAY drop or
coalesce samples, but `seq` MUST remain monotonic per channel so clients can
detect gaps. Clients MUST NOT assume lossless delivery. Servers MUST deliver
samples for one channel to one subscription in `seq` order.

## 10. Events

Events report discrete occurrences; telemetry reports sampled values. The
server pushes `notifications/event`:

- `name` (string, REQUIRED)
- `timestamp` (string, REQUIRED) — RFC 3339 UTC, as §9.2.
- `severity` (string, REQUIRED) — `"info"`, `"warning"`, or `"alarm"`.
- `data` (object, REQUIRED) — event-specific payload; MAY be `{}`.

Reserved event names in v0.1 (servers MUST use these names for these
meanings):

| Name | Meaning | `data` |
|---|---|---|
| `instrument/state_changed` | Operating state changed | `{state}`, instrument-defined states |
| `interlock/tripped` | A declared interlock tripped | `{interlock}` (its declared name) |
| `interlock/cleared` | A tripped interlock cleared | `{interlock}` |
| `measurement/stable` | A measurement reached stability (e.g. a balance settling) | `{channel, value}` |
| `error/occurred` | An error not attributable to one run | Error object (§11.2) |

Other event names are instrument-defined; vendor extensions MUST use the
`x-<vendor>/` prefix (§7.5). Servers whose `events` capability is `true`
MUST deliver every event to every operational session; there is no event
subscription in v0.1. Events MUST be delivered to a session in emission
order; delivery is best-effort, but events of severity `alarm` SHOULD NOT
be dropped.

## 11. Error Taxonomy

### 11.1 Codes

Standard JSON-RPC codes apply to protocol-level failures: `-32700` (parse
error), `-32600` (invalid request), `-32601` (method not found), `-32602`
(invalid params — malformed for the *method*, e.g. missing `command_id`),
`-32603` (internal JSON-RPC error).

Labwire domain errors use the JSON-RPC server-error range:

| Code | Category | Meaning | Retryable |
|---|---|---|---|
| -32000 | `validation` | Params violate a schema, a referenced entity is unknown, or a presented credential is rejected | no |
| -32001 | `unsupported` | Command not declared, or method belongs to a capability advertised `false` | no |
| -32002 | `busy` | At `max_concurrent_commands` capacity (retryable), or not yet initialized (servers SHOULD set `retryable: false` for this case) | yes |
| -32003 | `interlock` | Rejected or aborted because an interlock is tripped | no |
| -32004 | `hardware_fault` | The instrument reported a hardware failure | no |
| -32005 | `timeout` | The instrument did not respond internally in time | yes |
| -32006 | `canceled` | The run was canceled | no |
| -32007 | `not_cancelable` | Cancel requested for a run that cannot be canceled | no |
| -32008 | `internal` | Unexpected server error | no |

The "Retryable" column is the REQUIRED default for the `retryable` field;
servers MAY override it per error instance (e.g. a transient
`hardware_fault`).

When multiple rejection rules apply to one request, precedence is:
not-initialized (`-32002`) → method not found (`-32601`) → invalid method
params (`-32602`) → `unsupported` (`-32001`) → `validation` (`-32000`) →
`interlock` (`-32003`) → capacity `busy` (`-32002`).

### 11.2 Error object

Everywhere an error appears — JSON-RPC `error` member, or CommandStatus
`error` field — it is:

- `code` (integer, REQUIRED)
- `message` (string, REQUIRED) — human-readable, one line.
- `data` (object, REQUIRED for codes -32000..-32008):
  - `category` (string, REQUIRED) — from the table above.
  - `retryable` (boolean, REQUIRED) — whether the same request MAY succeed if
    retried without operator intervention. Agents SHOULD key retry policy off
    this field, not off `code`. Clients MUST treat errors lacking
    `data.retryable` (e.g. standard JSON-RPC errors) as not retryable.
  - `details` (object, OPTIONAL) — structured diagnostic payload.

Servers MUST NOT leak stack traces or internal paths in `message` or
`details`.

## 12. Signed Run Manifests

Every run that reaches a terminal state SHOULD produce a **run manifest**:
a portable, verifiable record of what instrument did what, with which
parameters, what came out, and how it ended. Manifests make results
attributable and tamper-evident.

Servers advertising the `manifests` capability (§6.1) MUST produce a
manifest for every terminal run. **How manifests are surfaced to consumers
is implementation-defined in v0.1** — no protocol method carries manifests;
the reference implementation writes a bundle (manifest + record stream) per
run to a local directory. A future protocol version may add a retrieval
method.

> **Conformance note:** manifest *format* is normative in v0.1; the
> reference implementation produces and verifies manifests starting at its
> M4 milestone (§14).

### 12.1 Manifest document

```json
<!-- example: manifest/document -->
{
  "manifest_version": "0.1",
  "protocol_version": "0.1",
  "run_id": "b7e0a1c2-4d5e-4f60-8a9b-0c1d2e3f4a5b",
  "instrument": {
    "manufacturer": "Labwire Project",
    "model": "SimBalance-120",
    "serial_number": "SIM-0003",
    "firmware_version": "0.1.0",
    "firmware_hash": "sha256:9f2b5c1d8e3a4f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8"
  },
  "command": {
    "name": "measure",
    "params": { "settle_timeout_s": 30.0 }
  },
  "status": "succeeded",
  "result": { "mass_g": 12.3456 },
  "data": {
    "digest_alg": "sha256",
    "digest": "306f739d0eb82314ad5783e7e673b8f61c56ebf938f521e80ffd2be0e5991450",
    "channels": ["mass"]
  },
  "timestamps": {
    "submitted": "2026-07-23T15:30:00.123456Z",
    "started": "2026-07-23T15:30:00.234567Z",
    "completed": "2026-07-23T15:30:12.345678Z"
  },
  "signer": {
    "alg": "ed25519",
    "public_key": "hSDwCYkwp1R0i33ctD73Wg2/Og0mOBr066SpjqqbTmo=",
    "key_id": "sha256:300c9c9603b92a4b39ed3958bf9240114804db4fd373012c0ca47432d63425ae"
  }
}
```

(`digest` is illustrative; `key_id` is the genuine SHA-256 of the example
`public_key`.)

All fields are REQUIRED unless marked otherwise:

- `manifest_version` (string) — `"0.1"` for this document.
- `protocol_version` (string) — the negotiated protocol version (§4).
- `run_id` (string) — the run's `command_id`.
- `instrument` (object) — the `identity` object from the descriptor (§7.1),
  verbatim.
- `command` (object) — `name` (string) and `params` (object): the submitted
  command, verbatim.
- `status` (string) — the run's terminal state (§8.1).
- `result` (any, present iff `status` is `succeeded` and the command
  returned a value) — the command's result, verbatim.
- `error` (object, present iff `status` is `failed`) — the Error object
  (§11.2).
- `data` (object):
  - `digest_alg` (string) — `"sha256"`, the only permitted v0.1 value.
  - `digest` (string) — lowercase hex SHA-256 of the run's **record
    stream**, defined below.
  - `channels` (array of strings) — every channel that produced samples
    during the run window; MAY be empty.
- `timestamps` (object) — `submitted`, `started`, `completed`: RFC 3339 UTC
  (§9.2), from the server's clock.
- `signer` (object):
  - `alg` (string) — `"ed25519"`, the only permitted v0.1 value.
  - `public_key` (string) — the 32-byte ed25519 public key, standard base64.
  - `key_id` (string) — `"sha256:"` + lowercase hex SHA-256 of the raw
    32-byte public key.

**Record stream.** The digest input is defined independently of
subscriptions and rate limits, so any two implementations recording the
same run produce identical digests. The record stream is the sequence, in
the server's emission order, of:

- for each sample produced on a channel listed in `data.channels` with
  timestamp in [`timestamps.started`, `timestamps.completed`]: the JCS
  canonicalization (§12.2) of
  `{"type": "sample", "channel": ..., "seq": ..., "timestamp": ..., "value": ...}`;
- for each event emitted in that window: the JCS canonicalization of
  `{"type": "event", "name": ..., "timestamp": ..., "severity": ..., "data": ...}`;

each record followed by exactly one `\n` (0x0A). Note the record objects
carry no `subscription_id` — they are production-side records, not
notifications. An empty record stream digests to the SHA-256 of zero bytes
(`e3b0c442…b855`). Bundles SHOULD include the record stream itself so
verifiers can recompute `digest`.

### 12.2 Canonicalization and signature

The signature is computed as:

1. Construct the manifest object **without** any `signature` field.
2. Canonicalize it using the JSON Canonicalization Scheme (JCS) [RFC 8785].
3. Sign the resulting UTF-8 bytes with ed25519 [RFC 8032].

The signed bundle is the manifest object plus a top-level `signature` field:
the 64-byte ed25519 signature, base64url-encoded without padding.

Verification: remove `signature`, canonicalize per JCS, verify against
`signer.public_key`, and check `signer.key_id` matches that key. Verifiers
MUST reject a bundle whose `key_id` does not match its `public_key`.

### 12.3 Keys

How a verifier comes to trust a public key is out of scope for v0.1. Servers
SHOULD generate a keypair on first run and persist it; operators SHOULD
record the `key_id` out of band (trust-on-first-use). This is stated plainly:
v0.1 manifests prove *integrity* (the record wasn't altered) and *key
continuity* (same signer as before), not *identity* (who the signer is). See
§13.

## 13. Security Considerations

v0.1 is designed for **trusted environments**: localhost or an isolated lab
network. Stated plainly:

- **Authentication is a stub.** The client MAY present `api_key` in
  `initialize` (§6.1); a server configured with a key MUST reject
  initialization on mismatch with error `-32000` (`validation`). There is no
  authorization model, no user identity, and no key rotation in v0.1.
- **Transport security.** Deployments that cross any network boundary SHOULD
  use `wss://` (TLS). The protocol itself provides no confidentiality.
- **Manifest guarantees** are limited to integrity and key continuity
  (§12.3). A manifest does not prove the physical sample, the operator, or
  the calibration state.
- **Agent-facing strings are untrusted input.** Descriptor fields
  (`title`, `description`, event payloads, error messages) flow into AI agent
  contexts. Agents and agent frameworks SHOULD treat them as data, never as
  instructions, and SHOULD NOT execute directives embedded in them. Servers
  MUST NOT require semantic interpretation of free-text fields for safe
  operation — safety-relevant behavior belongs in typed fields (interlocks,
  error categories, states).
- **Safety interlocks are not security boundaries.** A tripped interlock
  constrains the protocol (§8.5); a malicious server can lie about it.
  Physical safety MUST be enforced in the instrument, not in this protocol.

## 14. Conformance

### 14.1 Conformance levels

| Level | Requirements |
|---|---|
| **Core** | One transport (§5); `initialize`, `ping`, `notifications/initialized` (§6); `instrument/describe` (§7); command lifecycle with push status and polling (§8); error taxonomy (§11) |
| **Streaming** | Core + telemetry (§9) + events (§10) |
| **Signed** | Streaming + run manifests (§12) |

A server MUST document its level. A client MUST tolerate a server of any
level (the capability flags in the `initialize` *result* tell it what to
expect).

### 14.2 Reference implementation status (v0.1)

Honesty table — what the reference implementation in this repository
implements, by its milestone plan:

| Spec section | Status |
|---|---|
| §5.1 WebSocket transport | Implemented (M2) |
| §5.2 stdio transport | **Specified only** — no consumer yet; implementation unscheduled |
| §6 session lifecycle, §7 discovery, §8 commands, §9 telemetry, §10 events, §11 errors | Implemented (M2) |
| §12 signed manifests | Implemented (M4): bundle = `manifest.json` + `records.jsonl`, verified by `labwire verify` |
| §13 `api_key` stub | **Deferred — unscheduled** (no milestone in M2–M7 covers it) |
| In-memory transport (test-only; not a §5 transport) | Implemented (M2) |

This table is updated at each milestone commit; "Lands at" becomes
"Implemented" only when the milestone ships.

## 15. JSON Message Reference

Every protocol message, one example each. Examples are normative for shape.

Marker grammar: the first line *inside* each fenced JSON block is
`<!-- example: <name>/<kind> -->`, where `<name>` is a JSON-RPC method name
or one of the literals `error` and `manifest`, and `<kind>` is one of
`request`, `result`, `notification`, `notification-terminal`, `response`,
`document`, `signature-excerpt`. From milestone M2 onward, the reference
implementation's test suite extracts every marked block in this document
(including §12.1), strips the marker line, and round-trips the JSON through
the message model registered for `<name>` — failing if any example does not
round-trip. Manifest examples validate from M4; blocks whose kind is
`signature-excerpt` are validated only for the fields present.

Examples are independent snapshots, not one session timeline; `id`,
`command_id`, and hash/signature values are illustrative unless stated
otherwise.

### 15.1 initialize

```json
<!-- example: initialize/request -->
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocol_version": "0.1",
    "client_info": { "name": "labwire-client", "version": "0.1.0" },
    "capabilities": {}
  }
}
```

```json
<!-- example: initialize/result -->
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocol_version": "0.1",
    "server_info": { "name": "labwire-sim-pump", "version": "0.1.0" },
    "capabilities": { "telemetry": true, "events": true }
  }
}
```

### 15.2 notifications/initialized

```json
<!-- example: notifications/initialized/notification -->
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized",
  "params": {}
}
```

### 15.3 ping

```json
<!-- example: ping/request -->
{ "jsonrpc": "2.0", "id": 2, "method": "ping", "params": {} }
```

```json
<!-- example: ping/result -->
{ "jsonrpc": "2.0", "id": 2, "result": {} }
```

### 15.4 instrument/describe

```json
<!-- example: instrument/describe/request -->
{ "jsonrpc": "2.0", "id": 3, "method": "instrument/describe", "params": {} }
```

```json
<!-- example: instrument/describe/result -->
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "identity": {
      "manufacturer": "Labwire Project",
      "model": "SimPump-100",
      "serial_number": "SIM-0001",
      "firmware_version": "0.1.0",
      "firmware_hash": "sha256:6a3f0c1d8e2b4a5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7"
    },
    "commands": [
      {
        "name": "dispense",
        "title": "Dispense volume",
        "description": "Dispense a volume of liquid at a controlled flow rate.",
        "params_schema": {
          "type": "object",
          "properties": {
            "volume_ul": { "type": "number", "exclusiveMinimum": 0 },
            "rate_ul_min": { "type": "number", "exclusiveMinimum": 0 }
          },
          "required": ["volume_ul", "rate_ul_min"]
        },
        "unit_annotations": { "volume_ul": "uL", "rate_ul_min": "uL/min" },
        "returns_schema": {
          "type": "object",
          "properties": { "dispensed_ul": { "type": "number" } },
          "required": ["dispensed_ul"]
        },
        "estimated_duration_s": 30.0,
        "interruptible": true
      }
    ],
    "channels": [
      {
        "name": "flow_rate",
        "description": "Instantaneous flow rate.",
        "dtype": "float64",
        "unit": "uL/min",
        "sample_rate_hz_hint": 10.0
      }
    ],
    "interlocks": [
      {
        "name": "over_pressure",
        "description": "Trips when line pressure exceeds the safe limit. Clears when pressure drops below the limit.",
        "kind": "hard",
        "tripped": false
      }
    ],
    "max_concurrent_commands": 1
  }
}
```

### 15.5 command/submit

```json
<!-- example: command/submit/request -->
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "command/submit",
  "params": {
    "command": "dispense",
    "params": { "volume_ul": 500.0, "rate_ul_min": 1000.0 }
  }
}
```

```json
<!-- example: command/submit/result -->
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "command_id": "5f0c2f0a-7c1e-4d0b-9a63-2f3a1c8d9e4b",
    "status": "accepted"
  }
}
```

### 15.6 notifications/command_status

```json
<!-- example: notifications/command_status/notification -->
{
  "jsonrpc": "2.0",
  "method": "notifications/command_status",
  "params": {
    "command_id": "5f0c2f0a-7c1e-4d0b-9a63-2f3a1c8d9e4b",
    "status": "running",
    "progress": { "fraction": 0.4, "message": "200 of 500 uL dispensed" }
  }
}
```

```json
<!-- example: notifications/command_status/notification-terminal -->
{
  "jsonrpc": "2.0",
  "method": "notifications/command_status",
  "params": {
    "command_id": "5f0c2f0a-7c1e-4d0b-9a63-2f3a1c8d9e4b",
    "status": "succeeded",
    "result": { "dispensed_ul": 500.0 }
  }
}
```

### 15.7 command/status

```json
<!-- example: command/status/request -->
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "command/status",
  "params": { "command_id": "5f0c2f0a-7c1e-4d0b-9a63-2f3a1c8d9e4b" }
}
```

```json
<!-- example: command/status/result -->
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "command_id": "5f0c2f0a-7c1e-4d0b-9a63-2f3a1c8d9e4b",
    "status": "running",
    "progress": { "fraction": 0.8 }
  }
}
```

### 15.8 command/cancel

```json
<!-- example: command/cancel/request -->
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "command/cancel",
  "params": { "command_id": "5f0c2f0a-7c1e-4d0b-9a63-2f3a1c8d9e4b" }
}
```

```json
<!-- example: command/cancel/result -->
{
  "jsonrpc": "2.0",
  "id": 6,
  "result": {
    "command_id": "5f0c2f0a-7c1e-4d0b-9a63-2f3a1c8d9e4b",
    "status": "canceling"
  }
}
```

### 15.9 telemetry/subscribe

```json
<!-- example: telemetry/subscribe/request -->
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "telemetry/subscribe",
  "params": { "channels": ["flow_rate"], "max_rate_hz": 5.0 }
}
```

```json
<!-- example: telemetry/subscribe/result -->
{
  "jsonrpc": "2.0",
  "id": 7,
  "result": { "subscription_id": "sub-1" }
}
```

### 15.10 telemetry/unsubscribe

```json
<!-- example: telemetry/unsubscribe/request -->
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "telemetry/unsubscribe",
  "params": { "subscription_id": "sub-1" }
}
```

```json
<!-- example: telemetry/unsubscribe/result -->
{ "jsonrpc": "2.0", "id": 8, "result": {} }
```

### 15.11 notifications/telemetry

```json
<!-- example: notifications/telemetry/notification -->
{
  "jsonrpc": "2.0",
  "method": "notifications/telemetry",
  "params": {
    "subscription_id": "sub-1",
    "channel": "flow_rate",
    "seq": 1042,
    "timestamp": "2026-07-23T15:30:00.123456Z",
    "value": 999.7
  }
}
```

### 15.12 notifications/event

```json
<!-- example: notifications/event/notification -->
{
  "jsonrpc": "2.0",
  "method": "notifications/event",
  "params": {
    "name": "interlock/tripped",
    "timestamp": "2026-07-23T15:30:05.000001Z",
    "severity": "alarm",
    "data": { "interlock": "over_pressure" }
  }
}
```

### 15.13 Error response

```json
<!-- example: error/response -->
{
  "jsonrpc": "2.0",
  "id": 9,
  "error": {
    "code": -32002,
    "message": "Instrument is busy: 1 of 1 command slots in use",
    "data": { "category": "busy", "retryable": true }
  }
}
```

### 15.14 Signed manifest bundle

The manifest document example appears in §12.1. The signed bundle adds the
`signature` field:

```json
<!-- example: manifest/signature-excerpt -->
{
  "manifest_version": "0.1",
  "signature": "hcuNZWFGkEHDDTM1XZAs2Cj1YtqBhIWU93MOWkiPYbnhr1DAOFTZaKKCyBsnrLTogVCLYzp9nsdgnG5xqRDZBQ"
}
```

(All other manifest fields as §12.1; abbreviated here for length. The
`signature` value is illustrative, not a real signature over this example.)

## 16. Acknowledgments

Labwire borrows deliberately from prior art, with gratitude:

- **Model Context Protocol (MCP):** the initialize/initialized handshake,
  capability negotiation, slash-namespaced methods, newline-delimited stdio
  framing, and the no-batching stance. <!-- TODO-VERIFY: MCP spec revision
  in which batching was removed, before citing it in PRIOR_ART.md -->
- **SiLA 2:** the observable-command pattern — accept, then stream progress,
  then deliver a result — which shapes our command lifecycle, and the
  separation of commands from observable properties (our channels).
- **Bluesky / Ophyd:** the event-document mindset: timestamped, sequenced
  measurement documents, and the run-as-record idea that becomes our signed
  manifest.
- **OPC-UA LADS:** vocabulary for lab-device state machines and interlocks.
  <!-- TODO-VERIFY: confirm LADS's device state-machine/interlock
  vocabulary during the M7 prior-art review -->

A detailed comparison will land in `PRIOR_ART.md` (repository root,
milestone M7).

## 17. Changelog

- **0.1.0 (2026-07-23):** Initial draft. Protocol version `"0.1"`.

---

### References

- [JSONRPC] JSON-RPC 2.0 Specification, https://www.jsonrpc.org/specification
- [RFC 2119] Key words for use in RFCs to Indicate Requirement Levels
- [RFC 8174] Ambiguity of Uppercase vs Lowercase in RFC 2119 Key Words
- [RFC 3339] Date and Time on the Internet: Timestamps
- [RFC 8032] Edwards-Curve Digital Signature Algorithm (EdDSA)
- [RFC 8785] JSON Canonicalization Scheme (JCS)
- UCUM: The Unified Code for Units of Measure, https://ucum.org
- JSON Schema (draft 2020-12), https://json-schema.org
