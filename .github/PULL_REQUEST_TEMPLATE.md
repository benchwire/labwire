## What this changes

<!-- One paragraph. If it changes the protocol, name the spec sections. -->

## Checklist

- [ ] `make check` passes locally (ruff, pyright strict, full test suite)
- [ ] New behavior has a test that failed before the change
- [ ] Public APIs added or changed have docstrings with an example
- [ ] Spec and implementation still agree (JSON examples in SPEC.md are
      round-tripped in CI; if you changed message shapes, you changed both)
- [ ] No claim anywhere of compatibility with physical hardware that was
      not actually tested on that hardware; uncertain external claims are
      marked `TODO-VERIFY`
- [ ] Known gaps or limitations of this change are stated in the PR
      description or the docs, not left for reviewers to discover

## Honesty notes

<!-- What does this change NOT do? What did you not test? Saying so here
     is the house style, not a weakness. -->
