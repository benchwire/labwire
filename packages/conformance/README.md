# labwire-conformance

**Prove a Labwire server conformant, or see exactly why not.** Point the
runner at ANY server implementation over WebSocket and it exercises the
spec's normative requirements as executable checks, each tied to its spec
section, and reports the conformance level the server actually earns
(SPEC 15.1: Core, Streaming, Signed).

```bash
pip install labwire-conformance
labwire-conformance ws://127.0.0.1:9520
```

```
labwire conformance: ConformanceRig-1  (ws://127.0.0.1:9520)

  PASS  core.initialize.negotiates_0_3             SPEC 4, 6.1
  PASS  core.describe.units_mandatory              SPEC 7.2, 7.3
  PASS  safety.s2.refused_unconfirmed              SPEC 8.6
  PASS  safety.s3.refused_ungranted                SPEC 8.6
  PASS  resources.read_each                        SPEC 10.1, 10.2
  SKIP  core.lifecycle.exercise                    SPEC 8
        pass --exercise COMMAND to run one real command on a safe deployment
  ...

verdict: conformant at level CORE (SPEC 15.1)
```

## Safety by construction

Nothing here executes a handler uninvited. Every submitting check stops at
a refusal the server must issue **before** running anything: an S2 command
without confirmation, an S3 command without a grant, a reference that does
not resolve, parameters that fail validation. The one check that really
runs a command, `core.lifecycle.exercise`, is opt-in:

```bash
labwire-conformance ws://HOST:PORT \
  --exercise measure --params '{"settle_s": 0.5}' \
  --bundle-dir /path/to/server/run_bundles \
  --claim signed
```

`--bundle-dir` lets the signed-manifest checks find the bundle the
exercised run produced, verify it, and prove that a tampered copy fails.
The exit code is 0 only if the `--claim`ed level is fully proven.

## What "conformant" means

Binary pass/fail per normative requirement; no percentages. The verdict is
the highest SPEC 15.1 level at which every applicable check passes.
Checks for capabilities the server does not declare (resources, S2/S3
commands, telemetry channels) report `n/a` and never block; checks that
needed an opt-in you did not give report as skipped and DO block the
claim, telling you what was not proven. See SPEC 15 for the normative
definition and the honesty rules for stating a level.

## Limits

- WebSocket only. The stdio transport is specified but has no conformance
  runner yet (SPEC 15.2 says the same about the reference implementation).
- A passing report proves the checked behaviors, not the absence of every
  bug; the suite grows as findings do.
- The suite trusts the reference message models in `labwire-core` as the
  executable rendering of the spec's JSON reference (SPEC 16).

In CI this suite runs against the repository's own reference server; see
`packages/conformance/tests/test_reference_server.py`.
