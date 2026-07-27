"""Check outcomes and the conformance report."""

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

LEVELS = ("core", "streaming", "signed")


class Status(StrEnum):
    """Outcome of one check."""

    PASSED = "pass"
    FAILED = "fail"
    NOT_APPLICABLE = "n/a"
    UNEXERCISED = "unexercised"


@dataclass(frozen=True)
class CheckOutcome:
    """One check's result, tied to the spec section it tests.

    Example:
        >>> CheckOutcome("core.ping", "SPEC 6.4", "core", Status.PASSED, "").status
        <Status.PASSED: 'pass'>
    """

    check_id: str
    spec: str
    level: str
    status: Status
    detail: str


@dataclass
class Report:
    """Every outcome of one conformance run, and the verdict they add up to.

    ``n/a`` outcomes (the server does not declare the capability) never block
    a level. ``unexercised`` outcomes (the operator did not opt in to running
    a real command, or gave no bundle directory) block the CLAIM of the level
    they belong to without being failures: the report says exactly what was
    not proven.

    Example:
        >>> Report(instrument="X", target="ws://h").verdict()
        ('none', [])
    """

    instrument: str
    target: str
    outcomes: list[CheckOutcome] = field(default_factory=list)

    def add(self, outcome: CheckOutcome) -> None:
        """Record one outcome."""
        self.outcomes.append(outcome)

    def _scope(self, level: str) -> list[CheckOutcome]:
        wanted = LEVELS[: LEVELS.index(level) + 1]
        return [o for o in self.outcomes if o.level in wanted]

    def verdict(self) -> tuple[str, list[str]]:
        """The highest fully-proven level, plus what blocked anything higher.

        Returns ``(level_or_none, blockers)`` where blockers name the failed
        or unexercised checks standing between the server and the next level.
        """
        achieved = "none"
        blockers: list[str] = []
        for level in LEVELS:
            scope = self._scope(level)
            failed = [o for o in scope if o.status is Status.FAILED]
            unexercised = [o for o in scope if o.status is Status.UNEXERCISED]
            if failed or unexercised:
                blockers = [f"{o.check_id}: {o.status.value} ({o.detail})" for o in failed] + [
                    f"{o.check_id}: unexercised ({o.detail})" for o in unexercised
                ]
                break
            achieved = level
        return achieved, blockers

    def to_json(self) -> str:
        """The report as a JSON document."""
        level, blockers = self.verdict()
        payload: dict[str, Any] = {
            "instrument": self.instrument,
            "target": self.target,
            "verdict": {"level": level, "blockers": blockers},
            "checks": [
                {
                    "id": o.check_id,
                    "spec": o.spec,
                    "level": o.level,
                    "status": o.status.value,
                    "detail": o.detail,
                }
                for o in self.outcomes
            ],
        }
        return json.dumps(payload, indent=2)

    def render(self) -> str:
        """The report as terminal text."""
        marks = {
            Status.PASSED: "PASS",
            Status.FAILED: "FAIL",
            Status.NOT_APPLICABLE: "n/a ",
            Status.UNEXERCISED: "SKIP",
        }
        lines = [f"labwire conformance: {self.instrument}  ({self.target})", ""]
        for o in self.outcomes:
            line = f"  {marks[o.status]}  {o.check_id:42} {o.spec}"
            if o.detail and o.status is not Status.PASSED:
                line += f"\n        {o.detail}"
            lines.append(line)
        level, blockers = self.verdict()
        lines.append("")
        if level == "none":
            lines.append("verdict: NOT conformant at any level")
        else:
            lines.append(f"verdict: conformant at level {level.upper()} (SPEC 15.1)")
        for blocker in blockers:
            lines.append(f"  blocked above: {blocker}")
        return "\n".join(lines)
