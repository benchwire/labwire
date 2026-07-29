# Labwire Protocol Specification

**Version:** 0.4.0 (Draft)
**Protocol version string:** `"0.4"`
**Date:** 2026-07-27
**License:** Apache-2.0

---

## 1. Abstract & Status of This Document

Labwire is an open protocol for AI-controlled laboratory instruments. It gives
agents, both human-operated software and autonomous AI systems, a universal
way to **discover** an instrument's capabilities, **command** it, **stream**
its measurements, and receive **cryptographically signed** records of what was
done. The protocol is JSON-RPC 2.0 over WebSocket or stdio, with a capability
discovery model inspired by the Model Context Protocol (MCP). Version 0.4
makes cancellation honest: commands declare what cancel can physically do
(`cancel_semantics`, §8.3), acknowledgment is distinguished from
settlement, and a cancelled run's signed record states what actually
happened, including the case where nobody can confirm it. Version 0.3
added three things v0.2 could not express: **resources** (addressable, typed,
readable instrument state, such as a liquid handler's deck), **typed
references** (parameters that name a resource item rather than carrying an
uninterpreted string), and **operator grants** (an S3 authorization an agent
structurally cannot produce, bound to a command and to a digest of its exact
parameters).

This document is a **draft**. It is developed alongside a working reference
implementation; §15.2 states exactly which parts of this specification that
implementation realizes. Breaking changes are expected before 1.0, and v0.2
already makes some (§18).

## 2. Terminology & Conformance

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**,
**SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this
document are to be interpreted as described in BCP 14 [RFC 2119] [RFC 8174]
when, and only when, they appear in all capitals, as shown here.

- **Instrument Server (server):** a process that exposes exactly one
  instrument, real or simulated, over this protocol.
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
- **Event:** a discrete occurrence reported by the server (§11).
- **Interlock:** a declared safety condition which, while tripped, prevents
  command execution (§7, §8, §11).
- **Resource:** a named, URI-identified piece of instrument state, declared
  in the descriptor and read with `resource/read` (§7.6, §10).
- **Operator grant:** an out-of-band-provisioned authorization for one S3
  command with one exact parameter set (§8.6).
- **Run manifest:** a signed record of a completed run (§13).

All JSON field names defined by this protocol use `snake_case`. Unless
otherwise stated, unrecognized fields MUST be ignored by both parties
(forward compatibility).

## 3. Protocol Overview

Labwire uses JSON-RPC 2.0 [JSONRPC] messages over a bidirectional transport.
The client issues **requests**; the server answers with **responses** and
pushes **notifications** (command progress, telemetry, events). In v0.2 the
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
     │  resource/read {uri} ──────────────────────────▶│
     │◀──────────────── result: {revision, index, ...} │
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

A typical agent session: initialize → describe → read the resources the
descriptor declares → submit a command → watch pushed status until a terminal
state → read telemetry → repeat → disconnect.

### 3.1 JSON-RPC usage

- Messages MUST conform to JSON-RPC 2.0. The `jsonrpc` member MUST be `"2.0"`.
- Request `id`s MUST be JSON integers, and MUST NOT be reused within a
  session by the same sender.
- JSON-RPC **batch arrays MUST NOT be used.**
- A party receiving a request for a method it does not implement MUST respond
  with JSON-RPC error `-32601` (method not found).

## 4. Versioning & Negotiation

The protocol version is a string of the form `"MAJOR.MINOR"`. This document
specifies protocol version `"0.4"`.

- The client states its protocol version in `initialize`.
- The server replies with the protocol version **it will speak**: the highest
  version it supports that is compatible with the client's request.
- If the client does not support the version in the server's reply, the client
  MUST close the connection.
- Servers SHOULD accept any client version that shares their MAJOR version.
  For MAJOR version 0, the MINOR version carries compatibility significance:
  servers SHOULD reply with exactly `"0.4"` if they implement this document.

The specification document itself is versioned `MAJOR.MINOR.PATCH`
(this document: 0.4.0); PATCH revisions never change the wire protocol.

## 5. Transports

Labwire defines two transports. A conforming server MUST implement at least
one. All transports carry UTF-8 encoded JSON-RPC messages; framing is the
transport's job, and exactly one JSON-RPC message occupies one frame.

### 5.1 WebSocket

- Each JSON-RPC message MUST be sent as one WebSocket **text** frame
  containing one JSON object.
- Binary frames are reserved for future use (bulk data). In v0.2 a receiver
  MUST ignore binary frames.
- WebSocket protocol-level ping/pong frames MAY be used for keepalive by
  either party.
- Servers MAY serve plaintext `ws://` on loopback or isolated lab networks;
  deployments crossing any network boundary SHOULD use `wss://` (see §14).
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
`retryable: false` (§12.1). An `initialize` request received after
initialization has completed MUST be rejected with `-32600` (invalid
request).

`initialize` params:

- `protocol_version` (string, REQUIRED): the client's protocol version.
- `client_info` (object, REQUIRED): `{name, version}` identifying the client
  software.
- `capabilities` (object, REQUIRED): reserved for client capability flags;
  MAY be empty in v0.2.
- `api_key` (string, OPTIONAL): see §14.

`initialize` result:

- `protocol_version` (string, REQUIRED): the version the server will speak
  (§4).
- `server_info` (object, REQUIRED): `{name, version}` identifying the server
  software.
- `capabilities` (object, REQUIRED): server capability flags. Defined in
  v0.3: `telemetry` (boolean), `events` (boolean), `manifests`
  (boolean: the server produces signed run manifests, §13), `resources`
  (boolean: the server answers `resource/read`, §10), and `grants`
  (boolean: the server holds an operator grant store, §8.6). Absent flags
  default to `false`. A request for a method belonging to a capability the
  server advertised as `false` MUST be rejected with `-32001`
  (`unsupported`).

  A server that declares any `S3` command and advertises `grants: false` is
  **non-conforming and MUST refuse to start**: a server with hazardous
  commands and no way to authorize them is misconfigured, not permissive.
  Likewise a server whose commands carry `resource_ref` declarations (§7.2)
  MUST advertise `resources: true`.

After receiving the result, the client MUST send the
`notifications/initialized` notification before any other message. The
session is then **operational**.

### 6.2 Shutdown

There is no shutdown method. Either party ends the session by closing the
transport. On close, the server MUST treat all of the session's telemetry
subscriptions as cancelled. Commands already accepted continue to execute
(instruments are physical processes); their terminal states are simply no
longer observable in this session. They are recoverable via `command/status`
in a future session only if the server persists run state, which v0.2 does
not require.

### 6.3 Liveness

The `ping` request (empty params object) MUST be answered with an empty
result object as soon as it is received, at any point in the session,
including before initialization. Either party MAY send `ping` to detect a
stalled peer; it is the sole request a server may issue (§3).

### 6.4 Concurrent sessions

A server MAY accept multiple simultaneous sessions. Session-scoped rules:

- `notifications/command_status` for a run is delivered only to the session
  that submitted it. `command/status` polling MUST work from any session
  that presents the `command_id`.
- Telemetry notifications are delivered only to the subscribing session.
- Events (§11) are delivered to every operational session.
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
  - `firmware_hash` (string, OPTIONAL): `"sha256:<hex>"` of the firmware
    image, when known. Simulated instruments SHOULD hash their implementing
    code's version identity instead.

  This identity object is embedded verbatim in run manifests (§13).
- `commands` (array, REQUIRED): see §7.2.
- `channels` (array, REQUIRED): see §7.3.
- `interlocks` (array, REQUIRED): see §7.4.
- `resources` (array, REQUIRED): see §7.6. `[]` when the instrument exposes
  no resources; an instrument with no tree-shaped state loses nothing by
  saying so. There is deliberately no `resources/list` method: an
  instrument's resources are as much a property of its kind as its commands
  are, so they arrive inside `instrument/describe`, the request every client
  already makes, and discovering them is not a step an agent can skip.
- `max_concurrent_commands` (integer, OPTIONAL, default `1`): how many
  commands the instrument executes simultaneously (§8.4).

### 7.2 Command declaration

Each entry in `commands`:

- `name` (string, REQUIRED): unique within the instrument. Names beginning
  with `x-` are reserved for vendor extensions and MUST take the form
  `x-<vendor>/<name>` (§7.5); all other names are instrument-defined.
- `title` (string, REQUIRED): short human/agent-readable label.
- `description` (string, REQUIRED): what the command does, in enough detail
  for an agent to decide when to use it.
- `params_schema` (object, REQUIRED): a JSON Schema (draft 2020-12) object
  describing the command's `params`. The schema MUST be **closed**: every
  object within it MUST declare `"additionalProperties": false` (or name the
  schema its extra members follow), and every array MUST declare its `items`.
  An open schema permits a member nobody declared, and an undeclared member
  can be a quantity, so an open schema silently reopens the unit hole this
  section exists to close. The empty schema `{}` constrains nothing and MUST
  NOT be used; commands that take no parameters declare
  `{"type": "object", "additionalProperties": false}`. The same requirement
  applies to `returns_schema`.

  **Typed references.** A string-typed schema node inside `params_schema`
  MAY carry a `resource_ref` keyword declaring that its value names an item
  of a resource rather than being an uninterpreted string:

  ```json
  {
    "type": "string",
    "resource_ref": { "kind": "container", "enumerated_by": "labwire:deck" }
  }
  ```

  Both members are REQUIRED. `kind` is a registered or vendor-prefixed kind
  name (Appendix A) matched against the resolved entry's `kinds` array
  (§10.2). `enumerated_by` is the URI of a resource declared in this
  descriptor whose `item_kinds` contains `kind`; a declaration violating
  either condition is invalid, and servers MUST refuse to serve it. The
  keyword rides *inside* the schema deliberately: `params_schema` is the
  object that travels verbatim into agent tool schemas, so the pointer to
  where valid values live reaches the agent at the exact parameter it cannot
  fill, with no side table for an adapter to forget. Unknown keywords are
  ignored by ordinary JSON Schema validators, so the schema stays legal
  draft 2020-12.

  A node carrying `resource_ref` MUST NOT also declare a `pattern`: a
  pattern is satisfiable by invention, which is precisely the failure typed
  references exist to remove. `resource_ref` is permitted only inside
  `params_schema`, not in `returns_schema` or channel declarations, in
  v0.3. Reference values are validated against current resource state at
  submission (§10.4); the semantics of that check, and the error a failure
  produces, are protocol-defined so the reference vocabulary is shared by
  every instrument rather than invented per bridge.
- `unit_annotations` (object, REQUIRED): maps parameter name → **UCUM
  case-sensitive unit code** (e.g. `"mL/min"`, `"Cel"`, `"g"`, `"V"`).
  **Every parameter that carries a number MUST have an entry**, and
  dimensionless quantities MUST use `"1"` (UCUM unity). Commands whose
  parameters carry no numbers declare `{}`. Non-numeric parameters MAY be
  annotated but are not required to be.

  A parameter carries a number if a `number` or `integer` can appear
  anywhere in a conforming instance, not only when the parameter is itself
  one. In particular this includes arrays of numbers, arrays of arrays,
  fixed-length tuples (`prefixItems`), mappings whose values are numbers
  (`additionalProperties`), and any of these reached through `anyOf`,
  `oneOf`, `allOf`, or a local `$ref` into `$defs`. A `type` given as a list
  containing `number` or `integer`, and a `const` or `enum` pinning numeric
  values, also count. The unit belongs to the quantity, not to the container
  it arrived in: a command taking eight volumes as an array is annotated
  exactly like a command taking one.

  A parameter whose type is an **object with numeric fields** cannot be
  annotated under this scheme, because `unit_annotations` is keyed by
  parameter name and one code cannot describe fields of different kinds.
  Such a declaration MUST be rejected rather than served unannotated;
  flatten the fields into separate parameters. Per-path unit annotation is
  a candidate for a future version.

  Servers MUST reject their own malformed declarations at startup rather
  than serve an under-annotated descriptor; clients MAY reject a descriptor
  that violates this rule.
- `returns_units` (object, REQUIRED): the same mapping, under the same
  definition of carrying a number, for the command's result. A command whose
  result carries numbers without naming them (a bare number, an array, or a
  mapping) MUST declare at least one code. `{}` when the command returns
  nothing numeric.
- `qudt_quantity_kind` (object, OPTIONAL): maps the same parameter or
  result names to a QUDT `quantityKind` IRI or local name (e.g.
  `"VolumeFlowRate"`), for consumers doing dimensional reasoning.
- `safety_class` (string, OPTIONAL, default `"S1"`): one of `"S0"`,
  `"S1"`, `"S2"`, `"S3"`; see §8.6.
- `returns_schema` (object, OPTIONAL): JSON Schema for the command's
  `result` value.
- `estimated_duration_s` (number, OPTIONAL): typical wall-clock duration.
  Clients SHOULD use it to choose timeouts, and SHOULD apply their own
  default timeout when it is absent.
- `cancel_semantics` (string, OPTIONAL, default `"none"`): what
  `command/cancel` can honestly do to this command once it is running
  (§8.3). One of:
  - `"abort"`: the backend has a real halt path; cancellation may
    interrupt the physical operation, and the server can confirm whether
    the halt happened.
  - `"between_steps"`: the handler issues a sequence of backend
    operations; cancellation finishes the operation in flight and stops
    at the next boundary. It never interrupts a step.
  - `"none"`: once running, the command runs to completion. The physical
    action is already committed (on the wire to the device, or simply
    not stoppable), and pretending otherwise would be a lie.
  The default is deliberately the safe one: a command that does not say
  what cancel means cannot be cancelled mid-run. v0.3's `interruptible`
  boolean is REMOVED; a boolean could not distinguish aborting from
  stopping between steps, and its reference implementation abandoned the
  in-flight backend call, reporting `canceled` while hardware kept
  moving (SPEC-FINDINGS F10).
- `clears_interlocks` (array of strings, OPTIONAL): declared interlock
  names this command can clear. See §8.5: such a command remains submittable
  while a named interlock is tripped.

**Why units are mandatory.** An agent that cannot tell microlitres from
millilitres cannot be trusted with a syringe. Making the unit code a
declaration requirement rather than a documentation convention means an
agent, or a schema validator, can refuse an ambiguous instrument instead
of guessing. Labwire v0.1 treated units as optional prose; v0.2 does not.
This follows LAP's mandatory-UCUM design ([PRIOR_ART.md](../PRIOR_ART.md)).

Unit codes are validated for **presence**, not grammar: v0.2 servers MUST
require a non-empty string but are not required to parse UCUM syntax. Full
UCUM grammar validation is a roadmap item.
<!-- TODO-VERIFY: adopt a UCUM validation library, or vendor the grammar,
before declaring conformance to UCUM itself rather than to its code set. -->

Command results MUST be JSON-serializable values. This is what allows a
Labwire command to be exposed as a tool by agent frameworks with at most a
name mapping (e.g. MCP tools, where `params_schema` maps directly to a
tool's input schema; tool-name character sets may require renaming
<!-- TODO-VERIFY: allowed tool-name characters in the current MCP
revision -->).

### 7.3 Channel declaration

Each entry in `channels`:

- `name` (string, REQUIRED): unique within the instrument.
- `description` (string, REQUIRED).
- `dtype` (string, REQUIRED): one of `"float64"`, `"int64"`, `"bool"`,
  `"string"`. Values of `int64` channels MUST fit the IEEE-754
  exactly-representable integer range (|v| ≤ 2^53 − 1). Non-finite
  `float64` values (NaN, ±Infinity) MUST NOT be sent as samples; servers
  MUST either suppress such samples or report the condition as an event.
- `unit` (string, REQUIRED): the channel's **UCUM case-sensitive unit
  code**, a non-empty string. Use `"1"` (UCUM unity) for dimensionless
  channels. Presence is normative; grammar validation is not (§7.2).
- `qudt_quantity_kind` (string, OPTIONAL): QUDT `quantityKind` for this
  channel.
- `sample_rate_hz_hint` (number, OPTIONAL): the natural production rate.

### 7.4 Interlock declaration

Each entry in `interlocks`:

- `name` (string, REQUIRED): unique within the instrument.
- `description` (string, REQUIRED): the condition it protects against and
  how it clears.
- `kind` (string, REQUIRED): `"hard"` (trips autonomously in the
  instrument; cannot be cleared over the protocol) or `"soft"` (may be
  clearable by an instrument-defined command, see `clears_interlocks`,
  §7.2).
- `tripped` (boolean, REQUIRED): whether the interlock is tripped at the
  time the `instrument/describe` response is produced. Consumers MUST treat
  this as a snapshot, kept current only via the `interlock/tripped` and
  `interlock/cleared` events (§11).

Interlock behavior is specified in §8.5 and §11.

### 7.5 Vendor extensions

Command names and event names beginning with `x-<vendor>/` (e.g.
`x-sim/inject_fault`) are vendor extensions; the `x-` prefix is reserved
exclusively for this form. Servers MAY expose them; clients MUST NOT assume
their presence. Extension commands MUST still be declared in `commands` with
full schemas.

### 7.6 Resource declaration

A **resource** is addressable, typed, readable instrument state: the deck of
a liquid handler, the installed syringe of a pump. Commands describe what an
instrument can *do*; resources describe what *exists to do it to*. Each
entry in `resources`:

- `uri` (string, REQUIRED): the resource's identifier, unique within the
  instrument. See §10.1 for the scheme.
- `kind` (string, REQUIRED): what the resource is, from the registry
  (Appendix A) or vendor-prefixed (`<vendor>.<name>`).
- `title` (string, REQUIRED): short human/agent-readable label.
- `description` (string, REQUIRED): what the resource contains, what its
  index enumerates, and when it changes, in enough detail for an agent to
  decide when to read it. Servers SHOULD state here which command
  parameters draw their valid values from this resource's index.
- `item_kinds` (array of strings, REQUIRED): every kind that can appear in
  this resource's index (§10.2), so the closure of `resource_ref`
  declarations is checkable from the descriptor alone. `[]` for a resource
  with no index.
- `revision` (string, REQUIRED): the revision at the time the descriptor
  was produced, a snapshot exactly as `interlocks[].tripped` is (§10.3).
- `content_schema` (object, REQUIRED): a JSON Schema (draft 2020-12) object
  describing the `content` member of a read result. The closed-schema
  requirement of §7.2 applies unchanged.

  **Units inside content.** Every schema node in `content_schema` that
  describes a `number` or `integer` MUST carry a `unit` keyword holding a
  UCUM case-sensitive code (`"1"` for dimensionless):

  ```json
  { "type": "number", "unit": "uL" }
  ```

  Resource content is state, state carries quantities, and shipping a
  units-optional state format inside a units-mandatory protocol would
  reopen the hole §7.2 closed, one surface over. The `unit` keyword is
  scoped to `content_schema` in v0.3: it is NOT permitted in
  `params_schema` or `returns_schema`, whose units remain declared in
  `unit_annotations` and `returns_units`. Two annotation schemes exist, but
  they apply to disjoint surfaces and neither is optional, so there is
  never a question of which one to use. Unknown keywords are ignored by
  ordinary JSON Schema validators, so the schema stays legal draft 2020-12.
  The term `unit`, and the placement of semantics inside the data schema
  rather than in a side table, follow W3C Web of Things Thing Description
  practice (§17). <!-- TODO-VERIFY: the exact member name and section in
  WoT Thing Description 1.1 before citing it more precisely. -->

Resources are **read-only** in v0.3. Anything that changes instrument state
remains a command, so every state change stays classed, confirmed,
recorded, and signed; `set_well_volume` remains a command. A server MUST
refuse to start if any `resource_ref` in its commands names a resource not
declared here, or a `kind` absent from that resource's `item_kinds`: the
graph from parameter to kind to enumerating resource is provably closed
before a descriptor is ever served.

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

- `accepted`: validated and queued for immediate execution.
- `running`: executing.
- `canceling`: cancellation initiated; outcome not yet determined.
- `succeeded`, `failed`, `canceled`: **terminal** states. A run in a
  terminal state MUST NOT change state again.

Legal transitions: `accepted → running | canceling | failed`;
`running → succeeded | failed | canceling`;
`canceling → canceled | succeeded | failed`. The direct edges out of
`accepted` cover cancellation before start (§8.3) and interlock abort
(§8.5).

### 8.2 Submit and status

`command/submit` params: `command` (string, REQUIRED: a declared command
name), `params` (object, REQUIRED, validated against the command's
`params_schema`; MAY be `{}`), `confirmation` (string, OPTIONAL, required
for `S2` commands, see §8.6), `authorization` (object, OPTIONAL, required
for `S3` commands, see §8.6: `{"grant_id": "<id>"}`), and `if_revision`
(object, OPTIONAL: maps resource URI → the revision the client planned
against, see §10.5).

Submission checks run in the precedence order of §12.1. An undeclared
`command` name MUST be rejected with `-32001` (`unsupported`); `params`
violating the command's `params_schema` with `-32000` (`validation`); a
reference value that does not resolve with `-32010` (`unknown_reference`,
§10.4); a stale `if_revision` with `-32012` (`stale_revision`, §10.5); a
missing or unacceptable `confirmation` or `authorization` with `-32009`
(`confirmation_required`) or `-32011` (`authorization_required`) (§8.6).
In all these cases the server MUST NOT create a run. Otherwise the server
assigns a `command_id` (string, unique per instrument, RECOMMENDED: UUIDv4)
and responds `{command_id, status: "accepted"}`.

**Normalized parameters.** From validation onward the server MUST use the
post-validation parameter object, with schema defaults applied, as *the*
parameters of the run: it is what handlers receive, what the manifest
records as `command.params` (§13.1), and what the authorization digest is
computed over (§8.6). The digested thing and the recorded thing therefore
cannot disagree, and an auditor can recompute the digest offline from the
bundle. `confirmation`, `authorization`, and `if_revision` are envelope
fields, not parameters: they are never part of the normalized object or
the digest, so re-reading a resource after an operator approves a call
cannot invalidate the approval.

**Status is push-first.** On every state transition out of `accepted`, the
server MUST send `notifications/command_status` to the submitting session
(§6.4). The server MUST write the `command/submit` response before any
`notifications/command_status` for that run; the transition *into*
`accepted` is reported only by the submit response. A run's terminal
`notifications/command_status` MUST be sent after all telemetry and events
attributed to that run. The notification carries the **CommandStatus**
object:

- `command_id` (string, REQUIRED)
- `status` (string, REQUIRED): a state from §8.1.
- `progress` (object, OPTIONAL): `{fraction?, message?}`; `fraction` is
  0.0-1.0. Servers MAY additionally send `running` notifications that change
  only `progress`.
- `result` (any, OPTIONAL): present iff `status` is `succeeded`; conforms to
  the command's `returns_schema` if declared.
- `error` (object, OPTIONAL): an Error object (§12.2); present iff
  `status` is `failed`, except that servers MAY additionally attach an
  error with category `canceled` (code `-32006`) to `canceled` runs.
- `cancellation` (object, OPTIONAL): present on a terminal status iff a
  cancel was accepted for this run; see §8.3 for its fields and the
  claims it may and may not make.
- `resource_revisions` (array, OPTIONAL): on a **terminal** status, the
  resources this run changed, as `[{uri, revision}]` with each resource's
  revision after the run. This is the write-returns-the-new-revision
  pattern of HTTP conditional requests (§17): an agent that submits every
  change itself never needs to re-read a resource between steps, because
  each terminal status hands it the revision to plan the next step against.

`command/status` params `{command_id}` MUST return the current
CommandStatus; unknown `command_id` → error `-32000` (`validation`).
Polling MUST work even though push is required, so that simple clients need
no notification handling. To keep that guarantee meaningful, the server
MUST retain a run's terminal CommandStatus at least until the session that
submitted it closes; cross-session persistence is not required (§6.2).

### 8.3 Cancellation

`command/cancel` params: `{command_id}`.

**Acknowledgment is not settlement.** A `command/cancel` reply of
`canceling` means the server accepted the request; it claims nothing
about the physical world. Settlement is the run's terminal status, and
its `cancellation` block (below) states what actually happened. This
distinction exists because on real instruments a stop request returning
does not mean motion stopped (SPEC-FINDINGS F10).

Acceptance rules:

- A run in `accepted` (not yet running) MAY be cancelled regardless of
  `cancel_semantics`: dequeuing is not interruption. The server MUST
  reply with the current CommandStatus (typically `canceling`) and
  settle it `canceled`.
- A `running` run whose command declares `"abort"` or `"between_steps"`:
  the server MUST initiate cancellation per the declared semantics and
  reply with the current CommandStatus. The terminal state (`canceled`,
  or `succeeded`/`failed` if completion won the race) arrives via
  `notifications/command_status`.
- A `running` run whose command declares `"none"`: the server MUST
  reply with error `-32007` (`not_cancelable`), with
  `details.cancel_semantics: "none"` and `details.state: "running"`.
  A server MUST NOT accept such a cancel and ignore it: refusal is the
  only honest answer.
- A run that is already terminal or in `canceling`: error `-32007`, with
  `details.state` naming the state. An unknown `command_id` → error
  `-32000` (`validation`), as in `command/status`. Cancellation is
  therefore idempotent-safe, and "no such run" remains distinguishable
  from "cannot cancel".

**Settlement.** Any run that accepted a cancel MUST carry a
`cancellation` object on its terminal CommandStatus and in its signed
manifest (§13.1):

- `requested_at` (string, REQUIRED): when the cancel was accepted.
- `outcome` (string, REQUIRED):
  - `"halted"`: the backend CONFIRMED the physical stop. Only an
    `"abort"` command can settle this way, and only on positive
    confirmation.
  - `"halted_at_boundary"`: a `"between_steps"` command finished its
    in-flight step and stopped at the boundary.
  - `"ran_to_completion"`: completion won the race; the terminal status
    is `succeeded` or `failed`, and this block records that a cancel
    was pending when it finished.
  - `"unconfirmed"`: the stop was requested but the backend did not
    confirm the physical state within the server's settlement window.
    The terminal status is `canceled` and this is all the manifest
    asserts. This outcome is not a failure of the protocol; it is the
    truth, and servers MUST use it rather than guessing.
- `boundary` (object, present iff `outcome` is `"halted_at_boundary"`):
  `{completed_steps (integer), of_steps (integer or null), last
  (string)}`, the last step that completed.
- `detail` (string, OPTIONAL): free text, e.g. what the backend said or
  failed to say.

A terminal status of `canceled` asserts only that the run ended because
of cancellation; the physical claim lives entirely in
`cancellation.outcome`. A signed manifest MUST NOT contain a `canceled`
status without a `cancellation` block, and MUST NOT claim `"halted"`
without backend confirmation.

### 8.4 Concurrency

An instrument executes at most `max_concurrent_commands` (§7.1) runs
simultaneously. While at capacity, the server MUST reject `command/submit`
with error `-32002` (`busy`). The protocol defines **no server-side
queueing**: queueing, retry, and scheduling are client (agent) policy. The
capacity `busy` error is retryable (§12.2); servers MAY include
`details.retry_after_s` (number) as a backoff hint.

### 8.5 Interlocks

While any declared interlock is **tripped** (§11):

- New `command/submit` requests MUST be rejected with error `-32003`
  (`interlock`): except that a command whose `clears_interlocks` (§7.2)
  names a currently tripped interlock MUST be accepted. This is what makes
  soft-interlock recovery possible over the protocol.
- Runs in `accepted`, `running`, or `canceling` MUST transition to `failed`
  with an error of category `interlock`, unless the instrument can safely
  complete them (instrument-defined; completing is the exception, failing is
  the rule).

The server MUST emit `interlock/tripped` and `interlock/cleared` events
(§11). How an interlock clears is instrument-defined and MUST be stated in
its `description` (e.g. a hard interlock clears only at the instrument;
a soft interlock clears via a declared command).

### 8.6 Safety classes and confirmation

Every command carries a `safety_class` (§7.2). The taxonomy is adopted from
LAP ([arXiv:2606.03755](https://arxiv.org/abs/2606.03755); see
[PRIOR_ART.md](../PRIOR_ART.md)) so that instruments and agents crossing
between the two protocols classify actions the same way:

| Class | Meaning | Requires |
|---|---|---|
| `S0` | Emergency or protective operations (stop, vent, clear). Always permitted. | nothing |
| `S1` | Routine and reversible (read a value, set a setpoint). **Default.** | nothing |
| `S2` | Costly or irreversible (consumes reagent, destroys a sample). | `confirmation` |
| `S3` | Hazardous, capable of harming people or equipment. | an **operator grant** |

In v0.2 the two upper classes were gated by the same confirmation string,
so classifying a command `S3` changed what was printed and recorded and
nothing about what was permitted. v0.3 makes them different mechanisms:
`S2` takes a session confirmation an agent can hold; `S3` takes a grant an
agent structurally cannot produce.

Normative rules common to both:

- A server MUST NOT require confirmation or authorization for `S0`, and
  SHOULD NOT for `S1`.
- Servers MUST NOT downgrade a command's declared class at submission time.
- `S0` commands MUST remain submittable while an interlock is tripped
  (§8.5), since they are the means of recovery. (A command that clears an
  interlock therefore normally declares `S0` and lists it in
  `clears_interlocks`.)

**S2: confirmation.** Servers MUST reject a `command/submit` for an `S2`
command that carries no acceptable `confirmation` value, with error
`-32009` (`confirmation_required`), `retryable: false`, and
`data.details.safety_class` set to `"S2"`. What counts as acceptable is
deployment policy: a conforming server MAY accept any non-empty string,
MAY compare against a configured token, or MAY implement a stronger
scheme. A standing confirmation for a session of routine `S2` work is the
intended pattern.

**S3: operator grants.** An operator grant is a record in a server-side
**grant store**, provisioned out of band (configuration or environment,
e.g. a directory the server reads and an operator tool writes). Normative:

- **The protocol MUST NOT provide any method that creates, modifies,
  extends, enumerates, or reveals grants, and a conforming implementation
  MUST NOT add one as a vendor extension.** Whatever an agent can do over
  this protocol, minting authorization is not part of it.
- A grant binds, at minimum: the instrument's `serial_number`, one
  `command` name, one `params_digest`, a validity window
  (`[not_before, expires_at)`), and a use limit (`max_uses`, with a
  persistent use count). `params_digest` is
  `"sha256:" + lowercase-hex(SHA-256(JCS(params)))` over the **normalized**
  parameter object of §8.2, canonicalized per RFC 8785. Binding an operator
  authorization to the capability and to a digest of its canonical
  parameters is LAP's design ([arXiv:2606.03755](https://arxiv.org/abs/2606.03755)),
  adopted here with credit; LAP binds a JWS operator token, and v0.3 keeps
  the binding while deferring the signature (§14).
- A `confirmation` value MUST NOT satisfy an `S3` command, whatever it
  contains.
- On an `S3` submit that fails authorization, the server MUST reject with
  `-32011` (`authorization_required`), `retryable: false`, and
  `data.details` carrying at minimum: `safety_class`, `command`, a
  `reason` from the enum below, `params_digest`, `digest_alg`,
  `canonicalization`, and `mintable_by_agent: false`. Before refusing a
  submission whose only failure is a missing grant, the server SHOULD
  record a **pending authorization request**, capped in number and
  expiring, holding the command name, the normalized parameters verbatim,
  the digest, and the instrument identity, and SHOULD include its
  `request_id` and a server-configured `operator_instruction` in the error
  details. A pending request is a description of a request, not an
  authorization: recording one grants nothing. It exists so the operator's
  approval tool reads the parameters from the **server's own store**,
  never from a digest relayed through the agent that wants the approval.
- `reason` is one of: `absent` (no `authorization`, or a `confirmation`
  offered instead), `unsupported_scheme`, `unknown`, `command_mismatch`,
  `params_mismatch`, `instrument_mismatch`, `not_yet_valid`, `expired`,
  `exhausted`, `revoked`. `params_mismatch` is the reason that proves the
  binding is to parameters rather than an S3-shaped password: a valid,
  unexpired grant for the same command still fails on different values.
- On success the server MUST **atomically** consume one use (increment and
  persist the count) before creating the run; two concurrent submits MUST
  NOT both spend the last use of a grant, and a restart MUST NOT resurrect
  a spent one. Expiry remains the durable bound if the store is lost.
- A grant id is a bearer value. Servers SHOULD generate ids with at least
  128 bits of entropy, and MUST NOT write a grant id into any durable
  artifact (§13.1 records a digest of it instead).

**What a grant proves.** A verified grant proves that someone with write
access to this server's grant store approved this command name with these
exact parameter values, as the server itself recorded and displayed them,
within a time window and a bounded number of uses. It does **not** prove
who that person was, that the presenter is that person, or that anyone was
physically present: `issued_by` and `note` fields in a store are labels,
not authenticated identity. Cryptographic operator identity, a JWS profile
with key distribution and revocation, remains future work (§14,
[ROADMAP.md](../ROADMAP.md)). v0.2 proved deployment policy; v0.3 proves
deployment policy **plus parameter binding plus a bounded window**, and
still not identity. Deployments where identity matters should not treat a
v0.3 grant as an audit control over *who*.

## 9. Streaming Telemetry

### 9.1 Subscribe / unsubscribe

`telemetry/subscribe` params:

- `channels` (array of strings, REQUIRED): declared channel names. An
  undeclared name → error `-32000` (`validation`), and no subscription is
  created.
- `max_rate_hz` (number, OPTIONAL): per-channel ceiling on delivery rate.
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
- `seq` (integer, REQUIRED): per-channel sample sequence number assigned
  at *production*, independent of subscriptions: it MUST increment by
  exactly 1 for each successive sample the channel produces, so a jump in
  received `seq` indicates dropped samples. Two subscribers of the same
  channel observe the same `seq` for the same physical sample. `seq` is
  monotonic within one server process lifetime; clients MUST treat a
  decrease as a server restart, not a gap.
- `timestamp` (string, REQUIRED): RFC 3339 UTC with fractional seconds,
  e.g. `"2026-07-23T15:30:00.123456Z"`. This is the measurement time as the
  instrument knows it.
- `value` (REQUIRED): JSON value matching the channel's declared `dtype`.

### 9.3 Delivery semantics

Delivery is **best-effort**: under load or rate limits, servers MAY drop or
coalesce samples, but `seq` MUST remain monotonic per channel so clients can
detect gaps. Clients MUST NOT assume lossless delivery. Servers MUST deliver
samples for one channel to one subscription in `seq` order.

## 10. Resources

Instrument state that is a tree has to live somewhere. The descriptor is
static capability discovery; telemetry is unit-bearing scalars in a time
series; a deck that changes between runs fits neither, which is why v0.2
implementations smuggled it through ordinary command results that nothing
marked as special. Resources give it a first-class home: declared in
discovery (§7.6), read with one method, revisioned so staleness is
detectable, and indexed so typed references (§7.2) have something
protocol-defined to resolve against.

### 10.1 URIs

A resource identifier is `labwire:` followed by a rootless path (RFC 3986
`path-rootless`):

```
labwire:deck
labwire:deck/source_plate
labwire:deck/source_plate/A1
```

The first segment names a resource declared in the descriptor; further
segments name items within it. Segment text containing `/`, `?`, `#`, or
`%` MUST be percent-encoded. There is exactly one spelling of any URI: a
server MUST reject an alternative form that would resolve to the same
thing, rather than canonicalizing it.

**Child composition is protocol-defined, and instruments MUST NOT define
another.** An item URI is `<entry-uri> "/" <id>`, where `<id>` comes from
the read result's index (§10.2). This one rule is what keeps addressing
out of per-instrument convention: an agent that can read an index can
construct every legal reference on any conforming instrument, and there is
no grammar to learn or to guess. Ids are enumerated rather than templated
for the same reason: a template is a grammar.

The `labwire:` scheme is provisional and unregistered.
<!-- TODO-VERIFY: register the scheme with IANA, or confirm the provisional
form is acceptable, before 1.0. -->

### 10.2 resource/read

`resource/read` params: `uri` (string, REQUIRED): a resource URI declared
in the descriptor. Reading an item URI is not supported in v0.3; clients
read the resource and join on the index. An unknown, undeclared, or
malformed `uri` MUST be rejected with `-32010` (`unknown_reference`,
`reason: "unknown_resource"` or `"malformed_uri"`), so there is one story
about URIs that do not resolve rather than two.

Result:

- `uri` (string, REQUIRED): as requested.
- `kind` (string, REQUIRED): as declared.
- `revision` (string, REQUIRED): see §10.3.
- `read_at` (string, REQUIRED): RFC 3339 UTC timestamp of this read.
- `index_complete` (boolean, REQUIRED): whether `index` enumerates every
  resolvable reference. A server MAY set `false` for a resource it cannot
  enumerate exhaustively; it MUST still resolve references correctly, and
  a client MUST NOT infer non-existence from absence in an incomplete
  index.
- `index` (array, REQUIRED): the **reference index**. Each entry:
  - `uri` (string, REQUIRED): the entry's own URI.
  - `kinds` (array of strings, REQUIRED): every kind this entry satisfies,
    most specific first (a trough is `["trough", "container", "labware"]`).
    A reference declaring kind K resolves to this entry iff K is in this
    array; there is no subtyping graph in the protocol, the instrument
    declares the set.
  - `title` (string, OPTIONAL): a short label.
  - `children` (object, OPTIONAL): `{kinds, ids}`; the entry has one item
    per id, each with URI `<entry-uri>/<id>` and the given `kinds`. A
    96-well plate lists 96 ids rather than a range expression.
- `content` (REQUIRED): instrument-defined state conforming to the
  declared `content_schema` (§7.6). Where content describes a referenceable
  thing it MUST identify it by `uri`, so a client can join content to
  index.

A reference value V resolves iff some index entry E has `E.uri == V`
(satisfying `E.kinds`), or some entry E has `children` and
`V == E.uri + "/" + id` for an id in `E.children.ids` (satisfying
`E.children.kinds`).

Resources are read-only; there is no write method (§7.6). No pagination is
defined in v0.3; a resource whose index would be impractically large (a
1536-well plate is ~9 KB of ids and is fine; a plate hotel of thousands of
positions may not be) is a known open problem recorded in §15.2.

### 10.3 Revisions

`revision` is an opaque string that MUST change whenever a read of the
resource would return different `index` or `content`, and MUST NOT be
interpreted by clients beyond equality. RECOMMENDED construction is a
per-process nonce plus a counter; the reference implementation derives it
as a truncated hash of the canonicalized read result, which makes "the
driver forgot to bump it" impossible. Reference validation (§10.4) never
consults a revision, so a defective revision can at worst cost a missed
notification or a spurious `-32012`, never a wrong validation.

**Change notification** reuses the event channel (§11) under the reserved
name `resource/changed`, with `data: {uri, revision}`. Delivery is
best-effort exactly as §11 specifies; the revision in the payload lets a
client discard stale notifications. There is no per-resource subscription:
events are already broadcast to every operational session, and a second
push model for a handful of resources is surface without power. Because
events are written into active run records (§11), a signed manifest's
event stream also witnesses every deck change during the run.

### 10.4 Reference validation at submission

For each string location in the validated `params` whose schema node
carries `resource_ref` (including inside arrays), the server MUST resolve
the value against a **fresh** read of the declared `enumerated_by`
resource, per §10.2, checking that the resolved entry satisfies
`resource_ref.kind`. On the first failure the server MUST reject the
submission with `-32010` (`unknown_reference`), `retryable: false`, and
`data.details` carrying at minimum: `pointer` (an RFC 6901 pointer into
`params`, so the second element of an array is nameable), `parameter`,
`reference` (the offending value), `expected_kind`, `enumerated_by`, and
a `reason` from: `malformed_uri`, `unknown_resource`, `no_such_item`,
`kind_mismatch`. Servers SHOULD add `resolved_prefix` (the longest prefix
that did resolve) with its `resolved_kinds`, an OPTIONAL `did_you_mean`
list (capped, filtered by `expected_kind`), and `read`: a literal,
ready-to-send `resource/read` request object, so "I do not know what to
pass" becomes a call the agent can make without parsing prose.

Validation MUST use current state, not a cache keyed by revision: a
defective revision must not let a reference to moved labware pass. This
check is time-of-check-to-time-of-use: labware can move between validation
and execution, and v0.3 does not close that window (§14). `if_revision`
narrows it (§10.5).

### 10.5 Optimistic concurrency: if_revision

A client that plans against a resource read MAY assert its plan is still
valid by sending `if_revision` on `command/submit`: an object mapping each
resource URI it planned against to the `revision` it read. For each entry,
the server MUST compare against the resource's current revision and reject
on the first mismatch with `-32012` (`stale_revision`), `retryable:
false`, and `data.details` carrying `uri`, `submitted_revision`,
`current_revision`, and a ready-to-send `read` object. No run is created,
no confirmation is consumed, and no grant use is spent: staleness is
checked before authorization precisely so a stale plan never costs an
operator approval.

`if_revision` is an envelope field, never part of the normalized
parameters or the authorization digest (§8.2): an operator approves an
action, not a snapshot, and re-reading the deck after approval does not
invalidate a grant. A run's terminal status returns the new revisions
(§8.2), so a single agent driving an instrument can maintain freshness
without ever re-reading. What `if_revision` does not provide is a
reservation: between the check and a concurrent client's next write there
is no lock, and reservation leases remain future work (§14,
[ROADMAP.md](../ROADMAP.md)).

## 11. Events

Events report discrete occurrences; telemetry reports sampled values. The
server pushes `notifications/event`:

- `name` (string, REQUIRED)
- `timestamp` (string, REQUIRED): RFC 3339 UTC, as §9.2.
- `severity` (string, REQUIRED): `"info"`, `"warning"`, or `"alarm"`.
- `data` (object, REQUIRED): event-specific payload; MAY be `{}`.

Reserved event names in v0.3 (servers MUST use these names for these
meanings):

| Name | Meaning | `data` |
|---|---|---|
| `instrument/state_changed` | Operating state changed | `{state}`, instrument-defined states |
| `interlock/tripped` | A declared interlock tripped | `{interlock}` (its declared name) |
| `interlock/cleared` | A tripped interlock cleared | `{interlock}` |
| `measurement/stable` | A measurement reached stability (e.g. a balance settling) | `{channel, value}` |
| `error/occurred` | An error not attributable to one run | Error object (§12.2) |
| `resource/changed` | A resource's content or index changed (§10.3) | `{uri, revision}` |

Other event names are instrument-defined; vendor extensions MUST use the
`x-<vendor>/` prefix (§7.5). Servers whose `events` capability is `true`
MUST deliver every event to every operational session; there is no event
subscription in v0.3. Events MUST be delivered to a session in emission
order; delivery is best-effort, but events of severity `alarm` SHOULD NOT
be dropped.

## 12. Error Taxonomy

### 12.1 Codes

Standard JSON-RPC codes apply to protocol-level failures: `-32700` (parse
error), `-32600` (invalid request), `-32601` (method not found), `-32602`
(invalid params, malformed for the *method*, e.g. missing `command_id`),
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
| -32007 | `not_cancelable` | Cancel refused: terminal, already canceling, or the command declares `cancel_semantics: "none"` (details say which, §8.3) | no |
| -32008 | `internal` | Unexpected server error | no |
| -32009 | `confirmation_required` | An `S2` command was submitted without an acceptable `confirmation` (§8.6) | no |
| -32010 | `unknown_reference` | A `resource_ref` parameter value, or a `resource/read` URI, does not resolve in current resource state (§10.4) | no |
| -32011 | `authorization_required` | An `S3` command was submitted without a verifiable operator grant for these exact parameters (§8.6) | no |
| -32012 | `stale_revision` | An `if_revision` precondition did not match the resource's current revision (§10.5) | no |

The "Retryable" column is the REQUIRED default for the `retryable` field;
servers MAY override it per error instance (e.g. a transient
`hardware_fault`).

When multiple rejection rules apply to one request, precedence is:
not-initialized (`-32002`) → method not found (`-32601`) → invalid method
params (`-32602`) → `unsupported` (`-32001`) → `validation` (`-32000`) →
`unknown_reference` (`-32010`) → `stale_revision` (`-32012`) →
`interlock` (`-32003`) → capacity `busy` (`-32002`) →
`confirmation_required` (`-32009`) / `authorization_required` (`-32011`).

This order applies one principle consistently: **everything knowable
without an operator is checked first**, so an agent is never asked to
confirm, and a single-use grant is never spent, on a call that could not
have run. It moves `interlock` and capacity ahead of confirmation
relative to v0.2, which had already stated the principle for validation.
The reordering cannot deadlock recovery: interlock-clearing commands are
`S0` and exempt from the interlock check (§8.5).

### 12.2 Error object

Everywhere an error appears, JSON-RPC `error` member, or CommandStatus
`error` field: it is:

- `code` (integer, REQUIRED)
- `message` (string, REQUIRED): human-readable, one line.
- `data` (object, REQUIRED for codes -32000..-32012):
  - `category` (string, REQUIRED): from the table above.
  - `retryable` (boolean, REQUIRED): whether the same request MAY succeed if
    retried without operator intervention. Agents SHOULD key retry policy off
    this field, not off `code`. Clients MUST treat errors lacking
    `data.retryable` (e.g. standard JSON-RPC errors) as not retryable.
  - `details` (object, OPTIONAL): structured diagnostic payload.

Servers MUST NOT leak stack traces or internal paths in `message` or
`details`.

## 13. Signed Run Manifests

Every run that reaches a terminal state SHOULD produce a **run manifest**:
a portable, verifiable record of what instrument did what, with which
parameters, what came out, and how it ended. Manifests make results
attributable and tamper-evident.

Servers advertising the `manifests` capability (§6.1) MUST produce a
manifest for every terminal run. **How manifests are surfaced to consumers
is implementation-defined in v0.3**: no protocol method carries manifests;
the reference implementation writes a bundle (manifest + record stream) per
run to a local directory. A future protocol version may add a retrieval
method.

> **Conformance note:** the manifest format is normative; the reference
> implementation produces and verifies these bundles (§15.2).

### 13.1 Manifest document

```json
<!-- example: manifest/document -->
{
  "manifest_version": "0.4",
  "protocol_version": "0.4",
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
    "params": { "settle_timeout_s": 30.0 },
    "safety_class": "S1",
    "params_digest": "sha256:b8a66f00ce786f5fb861ea0d72562e611c8a0332c7ee5adc2dc88a4a2b527561"
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
`public_key`, and `params_digest` is the genuine digest of the example
`params` per §8.6.)

All fields are REQUIRED unless marked otherwise:

- `manifest_version` (string): `"0.4"` for this document. Verifiers MUST
  also accept `"0.2"` bundles, which lack the members introduced below;
  the format change breaks producers, not verifiers.
- `protocol_version` (string): the negotiated protocol version (§4).
- `run_id` (string): the run's `command_id`.
- `instrument` (object): the `identity` object from the descriptor (§7.1),
  verbatim.
- `command` (object): `name` (string), `params` (object): the
  **normalized** parameters of §8.2, with schema defaults applied, which
  are also the digest input, and `safety_class` (string): the class the
  server enforced for it (§8.6). In v0.2 `params` recorded the raw
  submission, so a command with defaulted optionals signed a manifest
  describing something other than what ran; recording the normalized
  object closes that, and lets an auditor recompute `params_digest`
  offline from the bundle alone. `params_digest` (string): the digest of
  §8.6, present for every run in a 0.3 manifest. All are covered by the
  signature, so a manifest records what safety posture applied to the run.
- `authorization` (object, present iff the command's class is `S2` or
  `S3`): how the run was authorized. For `S2`:
  `{"mode": "confirmation", "identity_verified": false}`. For `S3`:
  `mode` is `"grant"`, with `request_id` (string, OPTIONAL), an
  `expires_at` and `use_index` describing the grant use, free-text
  `issued_by` and `note` copied from the store **and labelled by this
  specification as unauthenticated**, `grant_digest` (string):
  `"sha256:"` + hex SHA-256 of the grant id, and `identity_verified`
  (boolean, REQUIRED): MUST be `false` in v0.3. The grant id itself MUST
  NOT appear: it is a bearer value and a signed bundle is a durable
  artifact. `identity_verified` exists so the honesty caveat of §8.6 is a
  machine-checkable wire fact rather than prose: verifiers MUST surface
  it, and a future version with cryptographic operator identity flips it.
- `cancellation` (object, present iff a cancel was accepted for the
  run): the settlement block of §8.3, covered by the signature. A signed
  manifest MUST NOT record a `canceled` status without it, and MUST NOT
  record `outcome: "halted"` the backend did not confirm: a manifest
  that asserts a physical halt the backend cannot vouch for is worse
  than no manifest at all.
- `resource_revisions` (array, OPTIONAL): for each resource the run
  changed, `{uri, revision_at_start, revision_at_end}`, so an auditor can
  ask whether state moved under the run.
- `status` (string): the run's terminal state (§8.1).
- `result` (any, present iff `status` is `succeeded` and the command
  returned a value): the command's result, verbatim.
- `error` (object, present iff `status` is `failed`): the Error object
  (§12.2).
- `data` (object):
  - `digest_alg` (string): `"sha256"`, the only permitted v0.2 value.
  - `digest` (string): lowercase hex SHA-256 of the run's **record
    stream**, defined below.
  - `channels` (array of strings): every channel that produced samples
    during the run window; MAY be empty.
- `timestamps` (object): `submitted`, `started`, `completed`: RFC 3339 UTC
  (§9.2), from the server's clock.
- `signer` (object):
  - `alg` (string): `"ed25519"`, the only permitted v0.2 value.
  - `public_key` (string): the 32-byte ed25519 public key, standard base64.
  - `key_id` (string): `"sha256:"` + lowercase hex SHA-256 of the raw
    32-byte public key.

**Record stream.** The digest input is defined independently of
subscriptions and rate limits, so any two implementations recording the
same run produce identical digests. The record stream is the sequence, in
the server's emission order, of:

- for each sample produced on a channel listed in `data.channels` with
  timestamp in [`timestamps.started`, `timestamps.completed`]: the JCS
  canonicalization (§13.2) of
  `{"type": "sample", "channel": ..., "seq": ..., "timestamp": ..., "value": ...}`;
- for each event emitted in that window: the JCS canonicalization of
  `{"type": "event", "name": ..., "timestamp": ..., "severity": ..., "data": ...}`;

each record followed by exactly one `\n` (0x0A). Note the record objects
carry no `subscription_id`: they are production-side records, not
notifications. An empty record stream digests to the SHA-256 of zero bytes
(`e3b0c442…b855`). Bundles SHOULD include the record stream itself so
verifiers can recompute `digest`.

### 13.2 Canonicalization and signature

The signature is computed as:

1. Construct the manifest object **without** any `signature` field.
2. Canonicalize it using the JSON Canonicalization Scheme (JCS) [RFC 8785].
3. Sign the resulting UTF-8 bytes with ed25519 [RFC 8032].

The signed bundle is the manifest object plus a top-level `signature` field:
the 64-byte ed25519 signature, base64url-encoded without padding.

Verification: remove `signature`, canonicalize per JCS, verify against
`signer.public_key`, and check `signer.key_id` matches that key. Verifiers
MUST reject a bundle whose `key_id` does not match its `public_key`.

### 13.3 Keys

How a verifier comes to trust a public key is out of scope for v0.2. Servers
SHOULD generate a keypair on first run and persist it; operators SHOULD
record the `key_id` out of band (trust-on-first-use). This is stated plainly:
v0.2 manifests prove *integrity* (the record wasn't altered) and *key
continuity* (same signer as before), not *identity* (who the signer is). See
§14.

## 14. Security Considerations

v0.3 is designed for **trusted environments**: localhost or an isolated lab
network. Stated plainly:

- **Authentication is a stub.** The client MAY present `api_key` in
  `initialize` (§6.1); a server configured with a key MUST reject
  initialization on mismatch with error `-32000` (`validation`). There is no
  authorization model, no user identity, and no key rotation in v0.2.
- **Authorization proves policy and binding, not identity.** An `S2`
  `confirmation` shows that whoever holds the deployment's token permitted
  this class of action. An `S3` grant (§8.6) proves more: someone with
  write access to the server's grant store approved this command with
  these exact parameter values, within a window and a use limit. Neither
  identifies *who*. A grant id is a bearer value on a transport that only
  SHOULD use TLS, replayable within its window by anyone who can read the
  traffic; single use and short expiry bound the damage, and the manifest
  records only its digest. On a single machine, an operator console and an
  agent running as the same user are not separated by anything this
  protocol can enforce; the grant store's write permissions are the actual
  boundary, and deployments where it matters put the store where the agent
  cannot write. Cryptographic operator identity: a JWS operator token
  signed over the task and the hash of its canonical parameters, as LAP
  specifies ([arXiv:2606.03755](https://arxiv.org/abs/2606.03755)): is the
  intended successor and is tracked in [ROADMAP.md](../ROADMAP.md). Until
  then, deployments MUST NOT rely on `confirmation` or grants as an
  accountability control over identity; `identity_verified: false` in
  every v0.3 manifest (§13.1) states this on the wire.
  <!-- TODO-VERIFY: settle on JWS profile + key distribution before
  implementing operator binding. -->
- **Reference validation is time-of-check-to-time-of-use.** A reference
  resolves at submission against state that can change before or during
  execution, and v0.3 provides no lock. `if_revision` (§10.5) narrows the
  window and never closes it; reservation leases are future work. v0.3
  ships **no lost-update protection**: two clients interleaving on one
  instrument can invalidate each other's plans without either seeing an
  error, unless both use `if_revision` and re-read on `-32012`.
- **Transport security.** Deployments that cross any network boundary SHOULD
  use `wss://` (TLS). The protocol itself provides no confidentiality.
- **Manifest guarantees** are limited to integrity and key continuity
  (§13.3). A manifest does not prove the physical sample, the operator, or
  the calibration state.
- **Agent-facing strings are untrusted input.** Descriptor fields
  (`title`, `description`, event payloads, error messages) flow into AI agent
  contexts. Agents and agent frameworks SHOULD treat them as data, never as
  instructions, and SHOULD NOT execute directives embedded in them. Servers
  MUST NOT require semantic interpretation of free-text fields for safe
  operation, safety-relevant behavior belongs in typed fields (interlocks,
  error categories, states).
- **Safety interlocks are not security boundaries.** A tripped interlock
  constrains the protocol (§8.5); a malicious server can lie about it.
  Physical safety MUST be enforced in the instrument, not in this protocol.

## 15. Conformance

### 15.1 Conformance levels

| Level | Requirements |
|---|---|
| **Core** | One transport (§5); `initialize`, `ping`, `notifications/initialized` (§6); `instrument/describe` (§7); command lifecycle with push status and polling (§8); error taxonomy (§12) |
| **Streaming** | Core + telemetry (§9) + events (§11) |
| **Signed** | Streaming + run manifests (§13) |

A server MUST document its level. A client MUST tolerate a server of any
level (the capability flags in the `initialize` *result* tell it what to
expect).

Some requirements are **capability-conditional** rather than level-bound:
they apply, at every level, exactly when the server declares the feature.
A server declaring resources must satisfy §10; a server declaring commands
with `resource_ref` parameters must satisfy §7.2 and §10.4; a server
declaring `S2`/`S3` commands must satisfy §8.6. A server declaring none of
these owes none of them.

### 15.2 Reference implementation status (v0.3)

Honesty table, what the reference implementation in this repository
implements:

| Spec section | Status |
|---|---|
| §5.1 WebSocket transport | Implemented |
| §5.2 stdio transport | **Specified only**: no consumer yet; implementation unscheduled |
| §6 session lifecycle, §7 discovery, §8 commands, §9 telemetry, §11 events, §12 errors | Implemented |
| §7.2/§7.3 mandatory UCUM codes | Implemented, presence enforced at declaration and on the wire; UCUM **grammar** not parsed |
| §7.2 `qudt_quantity_kind` | Implemented as a pass-through declaration; no QUDT reasoning |
| §7.2 typed references (`resource_ref`) | Implemented: shape at declaration, closure at server construction, resolution at submission |
| §7.6/§10 resources | Implemented: declaration, `resource/read`, derived revisions, `resource/changed`. **No pagination**; a very large index is an open problem. **No caching**: every reference validation re-reads |
| §8.6 `S2` confirmation | Implemented with a configured-token stub |
| §8.6 `S3` operator grants | Implemented: file-backed store, pending requests, atomic use counts, `labwire grant`. **Assumes the store lives where the agent cannot write; nothing in-protocol enforces that** |
| §8.6 cryptographic operator identity | **Not implemented**: `identity_verified` is `false` in every manifest; see §14 and ROADMAP.md |
| §10.5 `if_revision` | Implemented. **No reservation, no lost-update protection beyond it** |
| §8.3 `cancel_semantics` and settlement | Implemented: refusal on `"none"`, boundary settlement for `"between_steps"`, `"unconfirmed"` when a backend cannot confirm a halt |
| §13 signed manifests | Implemented: bundle = `manifest.json` + `records.jsonl`, verified by `labwire verify`; 0.2, 0.3, and 0.4 bundles all verify |
| §14 `api_key` stub | **Deferred, unscheduled** |
| In-memory transport (test-only; not a §5 transport) | Implemented |

This table is updated with each release.

### 15.3 Proving a level

`labwire-conformance` (the `packages/conformance` distribution) renders
these requirements as executable checks. Each check is binary pass/fail
and names the spec section it tests; the verdict is the highest level of
§15.1 at which every applicable check passes. There are no percentages: a
failed MUST is nonconformance, and the honest statement of partial
progress is the check list itself, not a score.

```bash
pip install labwire-conformance
labwire-conformance ws://HOST:PORT --claim core
```

Checks that would execute a command on the instrument are opt-in
(`--exercise COMMAND`, on a deployment where that is safe); everything
else stops at refusals the server must issue before running anything. The
signed-manifest checks need `--bundle-dir` pointing where the server
writes run bundles. A claim of "conformant at level X" made without the
opt-in checks is not supported by the tool, and the report says exactly
which proof is missing.

The suite trusts the reference message models as the executable rendering
of §16, and it grows as findings do: passing it proves the checked
behaviors, not the absence of every bug. In this repository's CI the
suite runs against the reference server on every commit.

## 16. JSON Message Reference

Every protocol message, one example each. Examples are normative for shape.

Marker grammar: the first line *inside* each fenced JSON block is
`<!-- example: <name>/<kind> -->`, where `<name>` is a JSON-RPC method name
or one of the literals `error` and `manifest`, and `<kind>` is one of
`request`, `result`, `notification`, `notification-terminal`, `response`,
`document`, `signature-excerpt`. The reference implementation's test suite
extracts every marked block in this document (including §13.1), strips the
marker line, and round-trips the JSON through the message model registered
for `<name>`: failing if any example does not round-trip. Blocks whose kind
is `signature-excerpt` are validated only for the fields present.

Examples are independent snapshots, not one session timeline; `id`,
`command_id`, and hash/signature values are illustrative unless stated
otherwise.

### 16.1 initialize

```json
<!-- example: initialize/request -->
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocol_version": "0.2",
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
    "protocol_version": "0.2",
    "server_info": { "name": "labwire-sim-pump", "version": "0.1.0" },
    "capabilities": { "telemetry": true, "events": true }
  }
}
```

### 16.2 notifications/initialized

```json
<!-- example: notifications/initialized/notification -->
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized",
  "params": {}
}
```

### 16.3 ping

```json
<!-- example: ping/request -->
{ "jsonrpc": "2.0", "id": 2, "method": "ping", "params": {} }
```

```json
<!-- example: ping/result -->
{ "jsonrpc": "2.0", "id": 2, "result": {} }
```

### 16.4 instrument/describe

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
          "additionalProperties": false,
          "properties": {
            "volume_ul": { "type": "number", "exclusiveMinimum": 0 },
            "rate_ul_min": { "type": "number", "exclusiveMinimum": 0 }
          },
          "required": ["volume_ul", "rate_ul_min"]
        },
        "unit_annotations": { "volume_ul": "uL", "rate_ul_min": "uL/min" },
        "returns_units": { "dispensed_ul": "uL" },
        "qudt_quantity_kind": {
          "volume_ul": "Volume",
          "rate_ul_min": "VolumeFlowRate"
        },
        "safety_class": "S2",
        "returns_schema": {
          "type": "object",
          "additionalProperties": false,
          "properties": { "dispensed_ul": { "type": "number" } },
          "required": ["dispensed_ul"]
        },
        "estimated_duration_s": 30.0,
        "cancel_semantics": "abort"
      },
      {
        "name": "abort",
        "title": "Abort motion",
        "description": "Stop the motor immediately and clear a stalled line.",
        "params_schema": { "type": "object", "additionalProperties": false },
        "unit_annotations": {},
        "returns_units": {},
        "safety_class": "S0",
        "clears_interlocks": ["over_pressure"]
      }
    ],
    "channels": [
      {
        "name": "flow_rate",
        "description": "Instantaneous flow rate.",
        "dtype": "float64",
        "unit": "uL/min",
        "qudt_quantity_kind": "VolumeFlowRate",
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
    "resources": [
      {
        "uri": "labwire:syringe",
        "kind": "consumable",
        "title": "Installed syringe",
        "description": "The syringe currently installed in the pump: its model, capacity, and how much it holds. Changes when a syringe is exchanged or the plunger moves.",
        "item_kinds": [],
        "revision": "b2c4e6a8-17",
        "content_schema": {
          "type": "object",
          "additionalProperties": false,
          "required": ["model", "capacity_ul", "installed_ul"],
          "properties": {
            "model": { "type": "string" },
            "capacity_ul": { "type": "number", "unit": "uL" },
            "barrel_diameter_mm": { "type": "number", "unit": "mm" },
            "installed_ul": { "type": "number", "unit": "uL" }
          }
        }
      }
    ],
    "max_concurrent_commands": 1
  }
}
```

An instrument with tree-shaped state and typed references declares them
together; a fragment of a liquid handler's descriptor:

```json
<!-- example: instrument/describe/result -->
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "identity": {
      "manufacturer": "PyLabRobot bridge (Labwire)",
      "model": "LiquidHandlerChatterboxBackend",
      "serial_number": "dilution-rig",
      "firmware_version": "0.3.0"
    },
    "commands": [
      {
        "name": "transfer",
        "title": "Transfer",
        "description": "Move liquid from one container into one or more others, aspirating and dispensing in one command.",
        "params_schema": {
          "type": "object",
          "additionalProperties": false,
          "required": ["source", "targets", "volumes_ul"],
          "properties": {
            "source": {
              "type": "string",
              "resource_ref": { "kind": "container", "enumerated_by": "labwire:deck" },
              "description": "The container to draw from. Must be a container listed in the index of resource labwire:deck; read that resource for the valid values."
            },
            "targets": {
              "type": "array",
              "minItems": 1,
              "items": {
                "type": "string",
                "resource_ref": { "kind": "container", "enumerated_by": "labwire:deck" }
              }
            },
            "volumes_ul": {
              "type": "array",
              "minItems": 1,
              "items": { "type": "number", "exclusiveMinimum": 0 }
            }
          }
        },
        "unit_annotations": { "volumes_ul": "uL" },
        "returns_units": { "total_volume_ul": "uL" },
        "safety_class": "S2",
        "cancel_semantics": "between_steps"
      }
    ],
    "channels": [],
    "interlocks": [],
    "resources": [
      {
        "uri": "labwire:deck",
        "kind": "deck",
        "title": "Deck",
        "description": "What is on the deck right now. Every container, tip site, labware and site a command parameter can name is listed in this resource's index. Changes whenever labware or liquid moves.",
        "item_kinds": ["labware", "plate", "tip_rack", "container", "tip_site", "site", "trash"],
        "revision": "9f3c1a4e-131",
        "content_schema": {
          "type": "object",
          "additionalProperties": false,
          "required": ["contents"],
          "properties": {
            "contents": {
              "type": "array",
              "items": {
                "type": "object",
                "additionalProperties": false,
                "required": ["uri", "volume_ul"],
                "properties": {
                  "uri": { "type": "string" },
                  "volume_ul": { "type": "number", "unit": "uL" },
                  "max_volume_ul": { "type": ["number", "null"], "unit": "uL" }
                }
              }
            }
          }
        }
      }
    ],
    "max_concurrent_commands": 1
  }
}
```

### 16.5 resource/read

```json
<!-- example: resource/read/request -->
{ "jsonrpc": "2.0", "id": 10, "method": "resource/read", "params": { "uri": "labwire:deck" } }
```

```json
<!-- example: resource/read/result -->
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": {
    "uri": "labwire:deck",
    "kind": "deck",
    "revision": "9f3c1a4e-131",
    "read_at": "2026-07-27T09:14:02.115430Z",
    "index_complete": true,
    "index": [
      {
        "uri": "labwire:deck/tips",
        "kinds": ["tip_rack", "labware"],
        "title": "tips",
        "children": { "kinds": ["tip_site"], "ids": ["A1", "B1", "C1", "D1"] }
      },
      {
        "uri": "labwire:deck/source_plate",
        "kinds": ["plate", "labware"],
        "title": "source_plate",
        "children": { "kinds": ["container"], "ids": ["A1", "A2", "B1", "B2"] }
      },
      { "uri": "labwire:deck/staging-0", "kinds": ["site"], "title": "staging-0" },
      { "uri": "labwire:deck/trash", "kinds": ["trash", "labware"], "title": "trash" }
    ],
    "content": {
      "contents": [
        { "uri": "labwire:deck/source_plate/A1", "volume_ul": 300.0, "max_volume_ul": 360.0 }
      ]
    }
  }
}
```

(`ids` arrays abbreviated; a real 96-well plate lists 96 two-character ids,
about 600 bytes.)

### 16.6 command/submit

```json
<!-- example: command/submit/request -->
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "command/submit",
  "params": {
    "command": "dispense",
    "params": { "volume_ul": 500.0, "rate_ul_min": 1000.0 },
    "confirmation": "operator-standing-grant-2026-07-26"
  }
}
```

`dispense` is declared `S2` (§8.6), so the `confirmation` field is
required; submitting the same request without it is rejected with `-32009`
(`confirmation_required`). An `S0` or `S1` command needs no such field.

An `S3` command takes an operator grant instead, and MAY carry
`if_revision` asserting the resource state the plan was made against
(§10.5). Note the reference-valued parameters and that no `confirmation`
appears: one would not satisfy `S3`.

```json
<!-- example: command/submit/request -->
{
  "jsonrpc": "2.0",
  "id": 18,
  "method": "command/submit",
  "params": {
    "command": "move_plate",
    "params": {
      "plate": "labwire:deck/dilution_plate",
      "to": "labwire:deck/staging-0"
    },
    "authorization": { "grant_id": "g-7f2a91c4" },
    "if_revision": { "labwire:deck": "9f3c1a4e-131" }
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

### 16.7 notifications/command_status

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
    "result": { "dispensed_ul": 500.0 },
    "resource_revisions": [
      { "uri": "labwire:syringe", "revision": "b2c4e6a8-18" }
    ]
  }
}
```

### 16.8 command/status

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

A cancelled run settles with its `cancellation` block (§8.3); here a
between-steps command stopped at a boundary:

```json
<!-- example: command/status/result -->
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "command_id": "5f0c2f0a-7c1e-4d0b-9a63-2f3a1c8d9e4b",
    "status": "canceled",
    "cancellation": {
      "requested_at": "2026-07-28T10:15:02.114Z",
      "outcome": "halted_at_boundary",
      "boundary": { "completed_steps": 1, "of_steps": 2, "last": "aspirate" },
      "detail": "in-flight aspirate finished; dispense was never issued"
    }
  }
}
```

And one whose backend never confirmed the stop; `unconfirmed` is the
honest settlement, not an error:

```json
<!-- example: command/status/result -->
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "command_id": "9a1b2c3d-4e5f-4a60-b7c8-d9e0f1a2b3c4",
    "status": "canceled",
    "cancellation": {
      "requested_at": "2026-07-28T10:16:40.020Z",
      "outcome": "unconfirmed",
      "detail": "device stop() returned but the status object never resolved"
    }
  }
}
```

### 16.9 command/cancel

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

Cancel against a running command that declares `cancel_semantics:
"none"` is refused, never accepted-and-ignored (§8.3):

```json
<!-- example: error/response -->
{
  "jsonrpc": "2.0",
  "id": 6,
  "error": {
    "code": -32007,
    "message": "aspirate is running and declares cancel_semantics 'none': the operation is already committed to the device",
    "data": {
      "category": "not_cancelable",
      "retryable": false,
      "details": { "cancel_semantics": "none", "state": "running" }
    }
  }
}
```

### 16.10 telemetry/subscribe

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

### 16.11 telemetry/unsubscribe

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

### 16.12 notifications/telemetry

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

### 16.13 notifications/event

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

A resource change rides the same channel under its reserved name (§10.3):

```json
<!-- example: notifications/event/notification -->
{
  "jsonrpc": "2.0",
  "method": "notifications/event",
  "params": {
    "name": "resource/changed",
    "timestamp": "2026-07-27T09:14:07.884120Z",
    "severity": "info",
    "data": { "uri": "labwire:deck", "revision": "9f3c1a4e-142" }
  }
}
```

### 16.14 Error response

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

A failed reference resolution names the failure precisely and hands the
agent the read that recovers (§10.4):

```json
<!-- example: error/response -->
{
  "jsonrpc": "2.0",
  "id": 12,
  "error": {
    "code": -32010,
    "message": "parameter /targets/1: 'labwire:deck/source_plate/A13' is not a container on this instrument",
    "data": {
      "category": "unknown_reference",
      "retryable": false,
      "details": {
        "pointer": "/targets/1",
        "parameter": "targets",
        "reference": "labwire:deck/source_plate/A13",
        "expected_kind": "container",
        "enumerated_by": "labwire:deck",
        "resolved_prefix": "labwire:deck/source_plate",
        "resolved_kinds": ["plate", "labware"],
        "reason": "no_such_item",
        "did_you_mean": ["labwire:deck/source_plate/A1", "labwire:deck/source_plate/B1"],
        "read": { "method": "resource/read", "params": { "uri": "labwire:deck" } }
      }
    }
  }
}
```

A refused `S3` submission records a pending request and tells the agent,
in a typed field, that it cannot mint what is missing (§8.6):

```json
<!-- example: error/response -->
{
  "jsonrpc": "2.0",
  "id": 19,
  "error": {
    "code": -32011,
    "message": "move_plate is S3 and requires an operator grant bound to these exact parameters; a confirmation string cannot authorize it",
    "data": {
      "category": "authorization_required",
      "retryable": false,
      "details": {
        "safety_class": "S3",
        "command": "move_plate",
        "reason": "absent",
        "request_id": "req-3f1c8d9e",
        "params_digest": "sha256:1c8d4fbb2e7a0f5d9c3b81a6e04f2d7c5b9e13a80f6c24d7e9b1a3c5f7d0e2b4",
        "digest_alg": "sha256",
        "canonicalization": "RFC8785",
        "mintable_by_agent": false,
        "operator_instruction": "On the instrument host run: labwire grant list, then labwire grant approve req-3f1c8d9e --ttl 15m --uses 1"
      }
    }
  }
}
```

A stale plan is refused before any confirmation or grant is spent
(§10.5):

```json
<!-- example: error/response -->
{
  "jsonrpc": "2.0",
  "id": 21,
  "error": {
    "code": -32012,
    "message": "labwire:deck has moved since this plan was made",
    "data": {
      "category": "stale_revision",
      "retryable": false,
      "details": {
        "uri": "labwire:deck",
        "submitted_revision": "9f3c1a4e-131",
        "current_revision": "9f3c1a4e-142",
        "read": { "method": "resource/read", "params": { "uri": "labwire:deck" } }
      }
    }
  }
}
```

### 16.15 Signed manifest bundle

The manifest document example appears in §13.1. The signed bundle adds the
`signature` field:

```json
<!-- example: manifest/signature-excerpt -->
{
  "manifest_version": "0.2",
  "signature": "hcuNZWFGkEHDDTM1XZAs2Cj1YtqBhIWU93MOWkiPYbnhr1DAOFTZaKKCyBsnrLTogVCLYzp9nsdgnG5xqRDZBQ"
}
```

(All other manifest fields as §13.1; abbreviated here for length. The
`signature` value is illustrative, not a real signature over this example.)

## 17. Acknowledgments

Labwire borrows deliberately from prior art, with gratitude:

- **Model Context Protocol (MCP):** the initialize/initialized handshake,
  capability negotiation, slash-namespaced methods, newline-delimited stdio
  framing, and the no-batching stance. <!-- TODO-VERIFY: MCP spec revision
  in which batching was removed, before citing it in PRIOR_ART.md -->
- **SiLA 2:** the observable-command pattern, accept, then stream progress,
  then deliver a result, which shapes our command lifecycle, and the
  separation of commands from observable properties (our channels).
- **Bluesky / Ophyd:** the event-document mindset: timestamped, sequenced
  measurement documents, and the run-as-record idea that becomes our signed
  manifest.
- **OPC-UA LADS:** vocabulary for lab-device state machines and interlocks.
  <!-- TODO-VERIFY: confirm LADS's device state-machine/interlock
  vocabulary against the published companion specification -->
- **LAP** ([arXiv:2606.03755](https://arxiv.org/abs/2606.03755)): the
  mandatory-UCUM discipline for every quantity (§7.2, §7.3), the S0-S3
  safety-class taxonomy with confirmation for costly and hazardous actions
  (§8.6), and the binding of an operator authorization to a capability and
  to a digest of its canonical parameters (§8.6). LAP binds a JWS operator
  token; v0.3 keeps the binding and defers the signature. Labwire and LAP
  are independent, convergent designs; these ideas are adopted from LAP
  with thanks, and no compatibility or endorsement is claimed.
- **W3C Web of Things Thing Description:** the placement of semantics
  inside an interaction affordance's data schema rather than in a side
  table, which decided `resource_ref` and the content-schema `unit`
  keyword (§7.2, §7.6), and the `unit` term itself.
  <!-- TODO-VERIFY: the exact member name and section in WoT Thing
  Description 1.1 before citing it more precisely. -->
- **JSON-LD:** the intuition that a value can be a typed link to a named
  node rather than a literal. Labwire v0.3 is **not** JSON-LD: there is no
  `@context`, `labwire:` URIs are not IRIs into a shared vocabulary, and
  `kind` is matched within one instrument against Appendix A alone. The
  intuition is borrowed; the machinery is deliberately not.
- **MCP resources:** the resource primitive itself, reduced to one read
  method and in-descriptor declaration (§10).
- **HTTP (RFC 9110):** conditional-request thinking behind `revision`,
  `if_revision`, and terminal-status revision reporting (§10.3, §10.5).
- **RFC 3986 / RFC 6901 / RFC 8785:** the URI shape, the error pointer
  form, and the canonicalization under every digest.

A detailed, honest comparison, including what these systems do better than
Labwire, lives in `PRIOR_ART.md` at the repository root.

## 18. Changelog

- **0.4.0 (2026-07-28):** Protocol version `"0.4"`. Cancellation made
  honest, prompted by field reports from an Opentrons Flex owner and the
  PyLabRobot maintainer (SPEC-FINDINGS F10). **Added:** per-command
  `cancel_semantics` (`"abort"`, `"between_steps"`, `"none"`; §7),
  acknowledgment-vs-settlement in §8.3, and the `cancellation`
  settlement block on terminal CommandStatus and in signed manifests
  (§13.1), including the first-class `"unconfirmed"` outcome for stops
  the backend cannot vouch for. **Removed (breaking):** the
  `interruptible` boolean; its cancellable-by-default semantics are the
  behavior the field reports indicted. Undeclared commands now default
  to `"none"`. Manifest version `"0.4"`; 0.3 and earlier bundles still
  verify.
- **0.3.0 (2026-07-27):** Protocol version `"0.3"`. Things, not only
  quantities. **Added:** resources: URI-identified, typed, readable
  instrument state declared in the descriptor (§7.6) and read with
  `resource/read` (§10), with derived revisions and the reserved
  `resource/changed` event; typed references: the `resource_ref` schema
  keyword, validated against current resource state at submission with the
  new error `-32010` (`unknown_reference`); operator grants for `S3`:
  out-of-band provisioned, bound to a command and the RFC 8785 digest of
  its normalized parameters (a binding adopted from LAP with credit),
  expiring and use-limited, refused with the new error `-32011`
  (`authorization_required`); optimistic concurrency: `if_revision` on
  submit with the new error `-32012` (`stale_revision`), and
  `resource_revisions` on terminal status. **Breaking:**
  `InstrumentDescriptor.resources` is REQUIRED; a `confirmation` no longer
  satisfies `S3`; submission precedence moves `interlock` and capacity
  ahead of confirmation and authorization (§12.1); the error `data`
  requirement extends to `-32012`; manifests are `"0.3"` with
  `command.params` now the **normalized** parameters (in v0.2 a command
  with defaulted optionals signed a manifest describing something other
  than what ran), plus `params_digest`, `authorization` with a REQUIRED
  `identity_verified: false`, and `resource_revisions`; the `unit` and
  `resource_ref` schema keywords are claimed, `unit` REQUIRED on numeric
  nodes in `content_schema` and forbidden in command schemas. Verifiers
  accept both `"0.2"` and `"0.3"` bundles.
- **0.2.1 (2026-07-27):** Corrective. The unit rule in §7.2 said "every
  numeric parameter (JSON Schema type `number` or `integer`)", which a
  reference implementation read literally, so an array of numbers carried no
  obligation and the guarantee that no quantity crosses the wire without a
  unit was false for every command that passes quantities as arrays. The rule
  now covers a parameter that carries a number *anywhere* in a conforming
  instance, and an object parameter with numeric fields, which the scheme
  cannot annotate, MUST be rejected rather than served. Protocol version
  stays `"0.2"`: no message shape changed, and a v0.2 descriptor that was
  correct under the old wording is still correct unless it was relying on the
  hole.
- **0.2.0 (2026-07-26):** Protocol version `"0.2"`. **Breaking:**
  `unit_annotations` and `returns_units` are now REQUIRED on every command
  and MUST carry a UCUM code for every numeric parameter and numeric result
  field (dimensionless = `"1"`); `ChannelSpec.unit` MUST be a UCUM code.
  **Added:** per-command `safety_class` (`S0`-`S3`, default `S1`, §8.6) with
  mandatory `confirmation` on `S2`/`S3` submissions and the new error
  `-32009` (`confirmation_required`); optional `qudt_quantity_kind`
  declarations; `command.safety_class` inside signed manifests (§13.1).
  Units and the safety taxonomy are adopted from LAP with credit (§17).
- **0.1.0 (2026-07-23):** Initial draft. Protocol version `"0.1"`.

---

## Appendix A. Kind registry

`kind` names without a dot are reserved for this registry; anything else
MUST take the form `<vendor>.<name>`, and clients MUST treat unrecognized
vendor kinds as opaque. This registry works the way UCUM codes do: an
instrument looks a name up, it does not invent one, because a kind two
instruments spell differently is the fragmentation typed references exist
to end.

Stated honestly: this registry is seeded from the single domain that
forced the feature (liquid handling) and is maintained by this project
alone. It is expected to grow one proven need at a time, and a governance
process is future work recorded in ROADMAP.md.

| Kind | Meaning |
|---|---|
| `deck` | The working area of a liquid handler |
| `labware` | Anything an instrument can hold or move |
| `plate` | A multi-well plate (also `labware`) |
| `tip_rack` | A rack of pipette tips (also `labware`) |
| `trough` | A single-cavity reservoir (also `container`, `labware`) |
| `trash` | A disposal target (also `labware`) |
| `lid` | A plate lid (also `labware`) |
| `container` | Holds liquid: a well, a tube, a trough cavity |
| `tip_site` | One spot of a tip rack |
| `site` | A position labware can stand on |
| `consumable` | An installed consumable: a syringe, a cartridge |

An index entry lists **every** kind it satisfies (§10.2), most specific
first, so a trough entry reads `["trough", "container", "labware"]` and a
reference declaring any of the three resolves to it. There is no subtyping
graph in the protocol; the instrument declares the set.

### References

- [JSONRPC] JSON-RPC 2.0 Specification, https://www.jsonrpc.org/specification
- [RFC 2119] Key words for use in RFCs to Indicate Requirement Levels
- [RFC 8174] Ambiguity of Uppercase vs Lowercase in RFC 2119 Key Words
- [RFC 3339] Date and Time on the Internet: Timestamps
- [RFC 8032] Edwards-Curve Digital Signature Algorithm (EdDSA)
- [RFC 8785] JSON Canonicalization Scheme (JCS)
- UCUM: The Unified Code for Units of Measure, https://ucum.org
- JSON Schema (draft 2020-12), https://json-schema.org
