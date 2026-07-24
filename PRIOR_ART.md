# Prior art, honestly

Labwire did not invent laboratory instrument control. Serious, mature
systems exist, several of them the product of years of consortium or
national-lab work, and Labwire borrows from them deliberately. This document
credits those borrowings and states plainly where Labwire differs — including
what the prior art does **better**.

Corrections are welcome; characterizations of external projects are made in
good faith and marked `TODO-VERIFY` where we have not confirmed a specific
claim against current upstream documentation.

## Model Context Protocol (MCP)

**What it is:** Anthropic's open protocol for connecting AI applications to
tools and data sources; JSON-RPC 2.0 with an initialize/capability handshake
and schema-described tools.

**What we borrowed:** the most, by far. The initialize/initialized handshake,
capability negotiation, slash-namespaced methods, newline-delimited stdio
framing, the no-batching stance, and above all the discovery philosophy:
*describe capabilities as JSON Schema so any agent can use them without
bespoke glue*. Labwire is in large part "MCP's discovery model applied to
physical instruments," and the bundled adapter maps Labwire commands to MCP
tools essentially one-to-one.

**How Labwire differs:** MCP models tools and resources generically; Labwire
specifies the instrument-domain semantics MCP has no opinion on — command
lifecycles with cancellation, sequenced telemetry channels with units, safety
interlocks, and signed run manifests.

## SiLA 2

**What it is:** the established open standard for lab-instrument
interoperability, developed by the SiLA consortium over many years; gRPC +
Protocol Buffers over HTTP/2, with a rich feature/command/property model.
<!-- TODO-VERIFY: current SiLA 2 version and transport details against
sila-standard.com before citing specifics -->

**What we borrowed:** the *observable command* pattern — accepted, then
progress, then result — shapes Labwire's command lifecycle, and SiLA's
feature model validates the commands-plus-properties split that Labwire
expresses as commands plus telemetry channels.

**What SiLA does better:** breadth and institutional adoption. SiLA has
certified implementations, vendor participation, and a governance process;
Labwire has none of those. If you need a consortium-backed standard with
vendor buy-in today, SiLA 2 is the serious choice.

**How Labwire differs:** Labwire is AI-agent-native (JSON Schema discovery an
LLM can consume directly, no protobuf toolchain), adds signed results as a
protocol feature, and optimizes for a five-minute zero-hardware onboarding
rather than certification.

## OPC-UA LADS

**What it is:** the Laboratory and Analytical Device Standard, an OPC UA
companion specification for analytical and lab devices, bringing industrial
automation's information-modeling rigor to the lab.
<!-- TODO-VERIFY: LADS scope and state-machine vocabulary against the
published companion specification -->

**What we borrowed:** vocabulary and seriousness about device state machines
and interlocks — the idea that safety conditions are part of the device
model, not an application afterthought.

**What LADS does better:** integration with plant/facility automation stacks,
historians, and the OPC UA security model. If your lab is an industrial
environment, LADS speaks its language.

**How Labwire differs:** OPC UA's power comes with weight. Labwire trades
that expressiveness for a protocol small enough to read in an afternoon and
implement in days.

## Bluesky / Ophyd

**What it is:** the Python data-acquisition ecosystem from the synchrotron
community (NSLS-II and collaborators): the Bluesky RunEngine orchestrates
experiment plans over Ophyd device abstractions, emitting structured
event-model documents.

**What we borrowed:** the event-document mindset — timestamped, sequenced
measurement records as the atomic unit of scientific data — directly shaped
Labwire's telemetry model and the run-manifest-as-record idea.

**What Bluesky does better:** orchestration depth. Plans, suspenders,
adaptive scans, and a decade of beamline production hardening make it the
strongest experiment-orchestration engine in open science. Labwire
deliberately has *no* orchestration layer — that is the agent's job.

**How Labwire differs:** Bluesky is a Python framework you embed in;
Labwire is a wire protocol any language or agent can speak, with discovery
and signing at the protocol level.

## PyLabRobot

**What it is:** an open-source, hardware-agnostic Python framework for lab
automation, strongest in liquid handling, with backends for multiple robot
vendors. <!-- TODO-VERIFY: current backend/device coverage against the
PyLabRobot documentation -->

**What we borrowed:** the conviction that open, hackable lab automation
should be a pip-install away, and the simulated-first development ethos.

**What PyLabRobot does better:** real hardware, today. PyLabRobot drives
actual robots in actual labs; Labwire v0.1 drives only its own simulators
and claims no real-hardware compatibility.

**How Labwire differs:** PyLabRobot is a device-control library with a
Python API; Labwire is a protocol with discovery, typed errors, interlocks,
and signed evidence, intended to sit *under* frameworks like it.

## Summary

| | Transport | Discovery | AI-native | Signed results | Real hardware | Setup |
|---|---|---|---|---|---|---|
| **Labwire v0.1** | JSON-RPC/WebSocket | JSON Schema | yes (MCP adapter) | yes (ed25519) | **no — simulated only** | ~5 min |
| SiLA 2 | gRPC/HTTP2 | Feature definitions | no | no | yes, certified | toolchain required |
| OPC-UA LADS | OPC UA | Information model | no | no | yes | industrial stack |
| Bluesky/Ophyd | Python (in-process) | Ophyd classes | no | no | yes, production | Python framework |
| PyLabRobot | Python (in-process) | Python classes | partial | no | yes | pip install |

The last row worth internalizing: every mature system above controls real
hardware; Labwire does not yet. What Labwire contributes is the combination —
agent-native discovery, protocol-level safety semantics, and portable signed
evidence — in a package small enough to evaluate in an afternoon.
