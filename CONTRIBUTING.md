# Contributing to Labwire

Thanks for looking under the hood. Labwire is a v0.3 draft protocol plus a
reference implementation; the most valuable contributions right now are
protocol feedback (there is an issue form for spec questions), independent
implementations run against `labwire-conformance`, bridge proposals, and
implementation bug reports.

## For external contributors

Fork, branch, PR against `main`. CI runs the full check suite on fork PRs
automatically; it needs no secrets and no maintainer pre-approval, so a
green fork PR means exactly what a green internal one does. If your change
touches the spec, the grant store, or signing, expect a slower, pickier
review (see CODEOWNERS): those paths are the product.

Where the implementation has taught us the spec was wrong, the lesson is
recorded in [SPEC-FINDINGS.md](SPEC-FINDINGS.md). If your work surfaces a
place the protocol strains, writing the finding is as valuable as writing
the fix, sometimes more.

## Getting set up

```bash
git clone https://github.com/benchwire/labwire.git && cd labwire
make setup    # uv installs Python 3.12 and every workspace package
make check    # ruff + pyright strict + full test suite: exactly what CI runs
```

Requirements: [uv](https://docs.astral.sh/uv/) (`curl -LsSf
https://astral.sh/uv/install.sh | sh` on Linux/macOS, or `brew install uv`).
uv manages Python itself; no system Python needed.

## How the repo works

- **uv workspace, one distribution per `packages/*` directory**, sharing the
  `labwire.*` namespace (PEP 420). Never create `src/labwire/__init__.py` in
  any package: it would break the shared namespace.
- **The spec is the source of truth.** Protocol changes start in
  [spec/SPEC.md](spec/SPEC.md); every JSON example in it is round-tripped
  through the implementation's models in CI, so spec and code cannot drift.
- **Tests first.** The project is built test-first; new behavior needs a
  failing test before the implementation. Coverage on `labwire.core` is
  CI-enforced at 85% (currently ~92%).
- **Strict everything:** pyright strict and ruff must pass. Every public API
  carries a docstring with an example.

## Honesty rules (non-negotiable)

- Never claim compatibility with a real vendor instrument that has not been
  tested against real hardware.
- Mark uncertain external claims `TODO-VERIFY` instead of asserting them.
- Comparisons to other systems must be honest, including what they do better
  ([PRIOR_ART.md](PRIOR_ART.md) sets the tone).

## Pull requests

- Conventional commits (`feat(core): ...`, `fix(sim): ...`, `docs(spec): ...`).
- One logical change per PR; include the failing-test-first evidence where
  applicable.
- `make check` must be green; CI runs it on Python 3.12 and 3.13.
- Protocol-affecting changes must update spec, models, and the conformance
  table (§15) together; if the change is checkable, add the check to
  `labwire-conformance` in the same PR.
- House prose style: plain sentences, no em or en dashes anywhere in the
  repo, and limitations stated where the reader will actually see them.

## License

By contributing you agree your contributions are licensed under
[Apache-2.0](LICENSE).
