"""Behavior specs: structural validity and no drift from runtime text.

The repo's ``.agents/behaviors/`` specs follow the Agent Behavior format
(agentbehavior.dev). Three things are enforced here:

1. Structural validity, mirroring the reference validator's checks
   (``packages/agentbehavior/src/index.ts`` at
   braintrustdata/agentbehavior commit 1866cff: NAME_PATTERN,
   MAX_NAME_LENGTH 64, MAX_DESCRIPTION_LENGTH 1024, directory-name
   match, exact BEHAVIOR.md file name, frontmatter delimited by bare
   ``---`` lines). The reference CLI is not published to npm as of
   2026-07-30 (no tags or releases either), so CI enforces the
   structural rules here; switch to the upstream CLI when it ships.
   Two checks are repo-local additions, not validator rules: the body
   must be non-empty, and the set of specs must equal EXPECTED_SPECS
   exactly (a deliberate inventory pin, so adding or removing a spec
   forces a conscious update here, including its pins).
2. Wording sync with tool descriptions: the safety and cancellation
   specs quote the exact sentences the MCP adapter serves in tool
   descriptions. Each pinned phrase must appear verbatim in BOTH the
   adapter constant and the spec body, so editing either side without
   the other fails.
3. Wording sync with served task status text: the evidence spec has no
   tool-description counterpart, so its pin is checked against the
   adapter's source, where the phrase is served in a task
   statusMessage.

PyYAML comes from this package's dev dependency group. The specs live
at the repo root, not in this package; when this test runs outside the
repo (an installed distribution), it skips.
"""

import re
from pathlib import Path
from typing import cast

import labwire.mcp.server as adapter
import pytest
import yaml
from labwire.mcp.server import (
    _CANCEL_NOTES,  # pyright: ignore[reportPrivateUsage]
    _SAFETY_NOTES,  # pyright: ignore[reportPrivateUsage]
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BEHAVIORS_DIR = REPO_ROOT / ".agents" / "behaviors"

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
# The closing delimiter must be a line that is exactly ---, mirroring the
# reference validator's /\r?\n---[ \t]*(?:\r?\n|$)/; a mere "---" prefix
# (e.g. "----" or "--- extra") is frontmatter-unclosed there.
FRONTMATTER_CLOSE = re.compile(r"\r?\n---[ \t]*(?:\r?\n|$)")

EXPECTED_SPECS = {
    "irreversible-command-approval",
    "hazardous-command-grants",
    "cancel-before-commit",
    "evidence-backed-reporting",
}

# Each entry: behavior name -> list of (adapter constant, pinned phrase).
# The phrase must appear verbatim (whitespace-normalized) in both the
# adapter text served to agents at runtime and the behavior spec text
# graders review, so the two voices cannot drift apart silently. Pins are
# full sentences wherever the spec quotes one, because short fragments
# survive meaning-inverting edits around them.
PINNED_PHRASES: dict[str, list[tuple[str, str]]] = {
    "irreversible-command-approval": [
        (
            _SAFETY_NOTES["S2"],
            "costly or IRREVERSIBLE, e.g. consumes reagent or destroys a sample",
        ),
        (
            _SAFETY_NOTES["S2"],
            "Requires a `confirmation` value; supply the operator-provided confirmation string.",
        ),
    ],
    "hazardous-command-grants": [
        (_SAFETY_NOTES["S3"], "HAZARDOUS, capable of harming people or equipment"),
        (_SAFETY_NOTES["S3"], "bound to this command and these exact parameter values"),
        (
            _SAFETY_NOTES["S3"],
            "refuse it and return a request id and the exact command a human operator must run",
        ),
        (_SAFETY_NOTES["S3"], "Report that to your operator and stop."),
        (_SAFETY_NOTES["S3"], "Never invent a grant id."),
    ],
    "cancel-before-commit": [
        (
            _CANCEL_NOTES["none"],
            "Once started this runs to completion; the operation is committed "
            "to the device and a cancel request will be refused. Decide before "
            "calling, not after.",
        ),
        (
            _CANCEL_NOTES["between_steps"],
            "partial physical effects (such as liquid already aspirated) remain",
        ),
        (_CANCEL_NOTES["abort"], "the physical state must be treated as unknown"),
    ],
}

# The evidence spec's runtime counterpart is not a tool description but the
# task statusMessage the adapter serves for a cancelled run; the phrase is
# pinned against the adapter source, where it appears in that literal.
SOURCE_PINNED_PHRASES: dict[str, list[str]] = {
    "evidence-backed-reporting": ["is retained on the instrument host"],
}


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _spec_dirs() -> list[Path]:
    if not BEHAVIORS_DIR.is_dir():
        pytest.skip("repo-root .agents/behaviors not present (installed distribution)")
    return sorted(p for p in BEHAVIORS_DIR.iterdir() if p.is_dir())


def _load(spec_dir: Path) -> tuple[dict[str, object], str]:
    # is_file() would accept a case variant on a case-insensitive
    # filesystem; the directory listing check does not.
    entries = {entry.name for entry in spec_dir.iterdir()}
    assert "BEHAVIOR.md" in entries, f"{spec_dir.name}: BEHAVIOR.md (exact name) is required"
    text = (spec_dir / "BEHAVIOR.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{spec_dir.name}: must start with YAML frontmatter"
    close = FRONTMATTER_CLOSE.search(text, 3)
    assert close is not None, f"{spec_dir.name}: frontmatter is not closed by a bare --- line"
    parsed: object = yaml.safe_load(text[4 : close.start()])
    assert isinstance(parsed, dict), f"{spec_dir.name}: frontmatter must be a mapping"
    return cast("dict[str, object]", parsed), text[close.end() :]


def test_expected_specs_present() -> None:
    assert {p.name for p in _spec_dirs()} == EXPECTED_SPECS


def test_pin_coverage_is_complete() -> None:
    # Every spec is pinned to served text one way or the other; shrinking
    # coverage must be an explicit edit here, not a silent deletion.
    assert set(PINNED_PHRASES) | set(SOURCE_PINNED_PHRASES) == EXPECTED_SPECS
    for pins in PINNED_PHRASES.values():
        assert pins
    for phrases in SOURCE_PINNED_PHRASES.values():
        assert phrases


def test_specs_are_structurally_valid() -> None:
    for spec_dir in _spec_dirs():
        frontmatter, body = _load(spec_dir)
        name = frontmatter.get("name")
        assert isinstance(name, str), f"{spec_dir.name}: name is required"
        assert name, f"{spec_dir.name}: name must be non-empty"
        assert len(name) <= MAX_NAME_LENGTH
        assert NAME_PATTERN.fullmatch(name), f"{name!r} violates the name pattern"
        assert name == spec_dir.name, "name must match the behavior directory name"
        description = frontmatter.get("description")
        assert isinstance(description, str), f"{spec_dir.name}: description is required"
        assert description.strip(), f"{spec_dir.name}: description must be non-empty"
        assert len(description) <= MAX_DESCRIPTION_LENGTH
        metadata = frontmatter.get("metadata")
        assert metadata is None or isinstance(metadata, dict)
        assert body.strip(), f"{spec_dir.name}: body must not be empty"


def test_spec_wording_matches_adapter_runtime_text() -> None:
    bodies = {spec_dir.name: _normalized(_load(spec_dir)[1]) for spec_dir in _spec_dirs()}
    for behavior, pins in PINNED_PHRASES.items():
        for adapter_text, phrase in pins:
            normalized = _normalized(phrase)
            assert normalized in _normalized(adapter_text), (
                f"pinned phrase no longer in the adapter text: {phrase!r}; "
                f"update {behavior}/BEHAVIOR.md and this pin together"
            )
            assert normalized in bodies[behavior], (
                f"pinned phrase missing from {behavior}/BEHAVIOR.md: {phrase!r}; "
                "the spec must quote the adapter's runtime sentence"
            )
    adapter_source = _normalized(Path(adapter.__file__).read_text(encoding="utf-8"))
    for behavior, phrases in SOURCE_PINNED_PHRASES.items():
        for phrase in phrases:
            normalized = _normalized(phrase)
            assert normalized in adapter_source, (
                f"pinned phrase no longer in the adapter source: {phrase!r}; "
                f"update {behavior}/BEHAVIOR.md and this pin together"
            )
            assert normalized in bodies[behavior], (
                f"pinned phrase missing from {behavior}/BEHAVIOR.md: {phrase!r}; "
                "the spec must quote the adapter's served sentence"
            )
