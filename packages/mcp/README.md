# labwire-mcp

The Labwire MCP adapter: connects to one or more Labwire Instrument Servers
over WebSocket and exposes every declared instrument command as an MCP tool,
so Claude and other MCP hosts can drive (simulated) lab hardware natively.

```bash
pip install labwire-mcp
labwire-mcp ws://127.0.0.1:9520 [ws://... ...]
```

See [examples/mcp-config.json](../../examples/mcp-config.json) for a
Claude-style MCP server configuration.

## MCP 2026-07-28, supported from day one

The adapter runs on the MCP Python SDK v2 and serves **both** protocol eras
from the same process with no configuration:

| Client | Handshake | Confirmation flow | Long commands |
|---|---|---|---|
| 2026-07-28 era | per-request metadata, `server/discover` | elicitation round trip (below) | returned as tasks, if the client declares the tasks extension |
| Handshake era (2025-11-25 and earlier) | classic `initialize` | `confirmation` parameter on the tool | ordinary blocking tool call |

Every adapter test runs twice, once per era, against the same server object.
List responses carry the cache hints the 2026 revision requires: tool and
resource lists cache generously (the surface is fixed for the life of the
process), resource reads not at all (a deck read is live instrument state).

## What a tool carries

Tool names are `<model>__<command>`. The command's `params_schema` (already
JSON Schema, SPEC 7.2) becomes the tool's `inputSchema` verbatim, UCUM unit
codes included. Each description states the command's safety class (S0 to
S3) and its cancel semantics sentence, so an agent knows what cancel can
physically do **before** committing to a call, not after.

## Human in the loop (2026-07-28 era)

An S2 tool call (costly or irreversible) that arrives without a
confirmation is not failed. The adapter returns `input_required` with an
elicitation showing the exact command and parameter values; the MCP host
surfaces it to the human; on approval the adapter injects the deployment's
standing confirmation and submits. Decline means nothing is submitted. The
agent never handles the confirmation token.

S3 (hazardous) goes further: the first call runs the server's refusal path,
and the elicitation carries the resulting request id plus the exact
`labwire grant approve` command an operator must run out of band. The human
types the freshly minted grant id into the elicitation; the retry submits
with that authorization. The adapter cannot pre-hold an S3 grant; each one
is bound to the exact command and parameter values.

The standing S2 confirmation is read from the `LABWIRE_MCP_CONFIRMATION`
environment variable at startup, never a CLI flag (flags are visible in
process listings). Without it, S2 calls keep the legacy parameter path.

Stated plainly: both paths prove a human approved **this call with these
parameters**. Neither proves **who**; the signed record says
`identity_verified: false` either way. Cryptographic operator identity is a
roadmap item, not a shipped feature.

Runnable end to end with zero hardware:

```bash
uv run examples/mcp_confirmation.py
```

## Long-running commands as tasks (experimental)

For 2026-era clients that declare the `io.modelcontextprotocol/tasks`
extension, a command whose `estimated_duration_s` is 10 s or more (the
threshold is configurable via `build_server`) returns a task instead of
blocking. The client polls `tasks/get`; `tasks/cancel` is served
cooperatively. Clients that do not declare the extension never see a task.

The SDK does not ship this extension, so it is implemented here from the
ext-tasks specification text. The 2026-07-28 changelog calls tasks an
official extension while the ext-tasks repository labels itself
experimental; we treat the text as less stable than core and say so.
TODO-VERIFY: the extension's final status.

The status mapping has two points that will surprise reviewers, both
required by the extension's own text:

- Task `failed` is reserved for JSON-RPC faults only. An instrument-level
  failure (hardware fault, interlock trip) is a task that **completed**,
  with `isError: true` inside the CallToolResult.
- A `cancelled` task carries no result field. When your `tasks/cancel`
  ends a run, the settlement outcome (SPEC 8.3: what physically happened)
  is summarized in `statusMessage`, and the signed run bundle stays on the
  instrument host. Every other terminal path delivers the full record.

`tasks/cancel` is cooperative by the extension's design, which maps
Labwire's cancel semantics exactly: a command declared
`cancel_semantics: "none"` acknowledges the cancel and runs to completion
(the ack promises nothing); `abort` and `between_steps` commands get a real
Labwire cancel and settle honestly. One trap from the spec: a client MAY
drop all task state after sending cancel and never fetch the outcome; the
signed bundle persists on the instrument host regardless.

Labwire's accepted, running, and canceling states all surface as `working`
(the extension has no queued or cancel-pending status); `statusMessage`
carries the truth. Tasks emit no progress notifications (the extension
forbids it); live telemetry stays on Labwire's own channels.

## Limitations

- The `input_required` round trip keys its `request_state` to in-process
  memory, and task state is in-process too. The stdio adapter is one
  process, so this is exact, but an adapter restart forgets pending
  approvals and task handles. The signed run bundles do not vanish; they
  are on the instrument host.
- The adapter holds the standing S2 confirmation for the deployment.
  Anyone who can reach the adapter's environment can approve S2 commands;
  neither confirmation path identifies the approver (see above).
- The tasks extension is experimental upstream and may change; ours tracks
  the published text, not a shipped SDK implementation.
- Verified against simulated instruments only, never physical hardware.
