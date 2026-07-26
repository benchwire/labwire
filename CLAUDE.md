# CLAUDE.md — Labwire project conventions

## What this is

An open protocol + reference implementation for AI-controlled laboratory
instruments ("MCP for lab equipment"): AI agents discover, command, and stream
data from scientific instruments, with ed25519-signed run manifests. The repo
must stay **impressive on GitHub** and **runnable by a stranger in under 5
minutes with zero hardware** (instruments are simulated).

## Fixed stack — do not deviate without asking the user

- Python 3.12 (CI also runs 3.13), uv for env/packaging, hatchling build backend
- Protocol: JSON-RPC 2.0 over WebSocket and stdio; capability discovery inspired
  by MCP; **no gRPC** in v0.1
- pydantic v2 for message models (approved), `websockets` for the WS transport
- Signing: ed25519 via **pynacl** (dependency added at M4, not before), RFC 8785
  (JCS) canonicalization before signing
- ruff (lint+format), pyright **strict**, pytest + pytest-asyncio
  (`asyncio_mode=auto`)
- GitHub Actions CI must be green on every milestone commit
- Apache-2.0 (patent grant matters for a standard)

## Repo layout

uv workspace monorepo; one distribution per `packages/*` dir sharing the
`labwire.*` import namespace (PEP 420):

- `packages/core` → `labwire-core` (M0) — server + client SDKs
- `packages/sim` → `labwire-sim` (M3) — simulated instruments
- `packages/drivers` → `labwire-drivers` (M3) — drivers speaking native wire
  protocols (SCPI/TCP, serial-style) against the sims
- `packages/cli` → `labwire-cli` (M4) — `labwire` CLI (`verify`, …)
- `packages/mcp` → `labwire-mcp` (M5) — MCP adapter
- `spec/` (M1), `examples/` (first example lands M2)

**NEVER create `src/labwire/__init__.py` in any package** — it would break the
PEP 420 namespace shared by all distributions. Only
`src/labwire/<subpkg>/__init__.py` exists.

Packages/directories are created at the milestone that fills them — no hollow
placeholder packages.

## Decisions already made (do not relitigate)

- GitHub repo `benchwire/labwire` is **public** (org `benchwire`; released with
  a fresh single-commit history). Push after every milestone; verify the
  Actions run is green.
- Git identity (repo-local, already configured):
  `Silous Ramelli <204268110+TheRoboMaster123@users.noreply.github.com>` —
  never commit with the user's personal email.
- The project name "labwire" is a placeholder; the user will rename later.
- Approved M0–M2 plan:
  `~/.claude/plans/project-labwire-placeholder-delegated-cerf.md`

## Milestone process

Work M0→M7 strictly in order. Per milestone: **one conventional commit**
(`chore:`, `docs(spec):`, `feat(core):`, …). After each milestone:

1. `make check` green locally
2. Summarize what exists; **list known gaps honestly**
3. Push; confirm GitHub Actions green before moving on

## Protocol v0.2 rules (enforced in code)

- Every numeric command parameter needs a UCUM code in `units=`, every named
  numeric result field one in `returns_units=` (`"1"` for dimensionless);
  channels need a non-empty UCUM `unit`. Violations raise `TypeError` at
  declaration time — that is deliberate, do not weaken it.
- Every command has a `safety_class` (S0–S3, default S1). Costly or
  irreversible actions are S2, hazardous ones S3; both require a
  `confirmation` on submit. Recovery paths (clearing an interlock, e-stop)
  are S0 so they stay submittable while interlocked.
- The UCUM discipline and the S0–S3 taxonomy come from LAP
  (arXiv:2606.03755) and MUST keep their credit in SPEC §16 and PRIOR_ART.md.
- Comparisons to other protocols stay factual and never disparaging; LAP in
  particular gets treated with respect. Never claim LAP compatibility or
  endorsement.

## Quality gates

- TDD where practical: failing test → minimal implementation → green → next
- Coverage ≥85% on `labwire.core`, CI-enforced (`fail_under` in root
  pyproject); pyright strict and ruff must pass at every commit
- Every public API has a docstring with an example (ruff `D` rules enforce on
  src; off for tests/examples)
- Simulators are first-class code, never throwaway mocks

## Honesty rules

- **Never** claim compatibility with a real vendor instrument model we have not
  tested against real hardware
- Mark uncertain external claims `TODO-VERIFY` instead of asserting them
- PRIOR_ART comparisons (M7) must be honest; credit what we borrow (MCP, SiLA 2,
  Bluesky/Ophyd, OPC-UA LADS)
- Conformance table in the spec states plainly what the reference
  implementation does and does not implement

## Non-goals for v0.1 — do not build

Fleet control plane, web UI, auth/RBAC beyond a stub API key, real hardware
drivers, cloud hosting, certification tooling.

## Environment notes

- macOS; Python is uv-managed (system python3 is 3.9 — never use it);
  `brew`-installed uv
- `gh` CLI is authenticated with push access to the `benchwire` org
- `make setup` = `uv sync --all-packages` (plain `uv sync` does NOT install
  workspace members — the root is `package = false`)
