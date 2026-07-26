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

## LAP — Lab Agent Protocol

**What it is:** "LAP: An Agent-to-Instrument Protocol for Autonomous Science"
([arXiv:2606.03755](https://arxiv.org/abs/2606.03755), Shiyanjia Lab, June
2026) — a design specification for exactly the edge Labwire targets,
positioned as the third edge alongside MCP (agent-to-tool) and A2A
(agent-to-agent). Its four primitives are carefully thought through:
**InstrumentCard** (a signed JSON-LD capability and physical-limit
description, profiled on W3C WoT Thing Description 1.1 and served at
`/.well-known`, with per-capability safety class, physical limits,
interlocks, intent tags, and calibration block); first-class **reservation
leases** (request/renew/release, epochs, exclusive vs shared-read); a
**safety fence** of classes S0 (emergency operations, always permitted)
through S3 (hazardous; requires a JWS operator token cryptographically bound
to the exact task and the SHA-256 of canonical params), with error codes in
a -33xxx JSON-RPC block; and **MeasurementResult** (mandatory UCUM unit
codes and QUDT quantityKind on every value, calibration reference,
uncertainty model, provenance manifest, signature). Transport is JSON-RPC
2.0 over HTTPS + SSE with an A2A-compatible surface.

**Status:** LAP is explicitly a specification: the paper states it has no
implemented status, and its own comparison table lists its running
implementation as "none (design)".

**What Labwire adopts, with credit:** Labwire and LAP are independent
convergent designs — JSON-RPC, capability discovery, declared interlocks,
signed results — which we take as evidence the shape is right. Protocol
v0.2 adopts two of LAP's ideas outright because they are better than what
Labwire v0.1 had: **mandatory UCUM unit codes** on every quantity, and the
**S0–S3 safety-class taxonomy** with confirmation required for S2/S3
commands. Labwire v0.2 implements a simple confirmation token; LAP's
cryptographically bound operator tokens are the more complete design and
are on Labwire's roadmap.

**What LAP has that Labwire does not:** reservation leases, calibration
blocks, JSON-LD/WoT-profiled capability documents, and the full operator
token binding — all roadmap candidates for Labwire, listed rather than
claimed.

## SCP — Science Context Protocol

**What it is:** "SCP: Accelerating Discovery with a Global Web of
Autonomous Scientific Agents"
([arXiv:2512.24189](https://arxiv.org/abs/2512.24189), Shanghai AI Lab,
December 2025) — extends MCP with a centralized SCP Hub registry, persistent
experiment-lifecycle objects, and a device-driver abstraction, deployed on
the Intern-Discovery platform with more than 1,600 integrated tool
resources.

**What SCP does better:** operating at platform scale, today, with a
production deployment orders of magnitude beyond anything Labwire has run.

**How Labwire differs:** architecture. SCP is hub-mediated — instrument
access proxies through the Hub — where Labwire is deliberately hub-less:
an agent speaks directly to an instrument server with no registry or
central service in the path. Both shapes have merit; a hub gives fleet
governance, a peer protocol gives five-minute adoption and no
infrastructure dependency.

## MCP tool wrapping (Hein lab)

Work from the Hein lab (NeurIPS 2025 AI4Mat workshop) wraps self-driving-lab
instrument APIs directly as MCP tools — independent confirmation that
agent-native instrument interfaces are the need of the moment.
<!-- TODO-VERIFY: full citation (authors/title) before adding a formal
reference --> Plain MCP tool schemas, however, carry no physical typing,
safety classes, reservations, or signed results; that gap is precisely what
both LAP and Labwire, in their different ways, exist to fill.

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

| | Transport | Discovery | AI-native | Signed results | Running implementation | Setup |
|---|---|---|---|---|---|---|
| **Labwire v0.2** | JSON-RPC/WebSocket | JSON Schema | yes (MCP adapter) | yes (ed25519) | yes — **simulated instruments only** | ~5 min |
| LAP | JSON-RPC/HTTPS+SSE | JSON-LD (WoT TD) | yes (A2A surface) | yes (spec) | none (design) — per its own table | n/a |
| SCP | MCP-based, hub-mediated | Hub registry | yes | no | yes, platform-scale | platform onboarding |
| SiLA 2 | gRPC/HTTP2 | Feature definitions | no | no | yes, certified, real hardware | toolchain required |
| OPC-UA LADS | OPC UA | Information model | no | no | yes, real hardware | industrial stack |
| Bluesky/Ophyd | Python (in-process) | Ophyd classes | no | no | yes, production beamlines | Python framework |
| PyLabRobot | Python (in-process) | Python classes | partial | no | yes, real robots | pip install |

Two honest caveats worth internalizing: the mature pre-agent systems all
control real hardware and Labwire does not yet; and among the agent-native
efforts, LAP's design goes further than Labwire's in several dimensions
(leases, calibration, operator-token binding) while Labwire is the one you
can clone and run this afternoon. What Labwire contributes is the working
combination — agent-native discovery, protocol-level safety semantics, and
portable signed evidence — as running, tested code.
