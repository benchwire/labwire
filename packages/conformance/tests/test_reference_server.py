"""The CI proof: the reference implementation passes its own conformance suite."""

from pathlib import Path

from labwire.conformance import Report, RunOptions, Status, run_suite


def _by_id(report: Report) -> dict[str, Status]:
    return {o.check_id: o.status for o in report.outcomes}


async def test_reference_server_is_signed_conformant(rig_url: tuple[str, Path]) -> None:
    """Every check passes against the reference server: level SIGNED, no blockers."""
    url, manifest_dir = rig_url
    report = await run_suite(
        url,
        RunOptions(
            exercise="measure",
            exercise_params={"settle_s": 0.1},
            bundle_dir=manifest_dir,
        ),
    )
    statuses = _by_id(report)
    failed = {cid: s for cid, s in statuses.items() if s is Status.FAILED}
    assert not failed, f"reference server failed conformance checks: {failed}"
    assert statuses["safety.s2.refused_unconfirmed"] is Status.PASSED
    assert statuses["safety.s3.refused_ungranted"] is Status.PASSED
    assert statuses["resources.read_each"] is Status.PASSED
    assert statuses["references.unknown_ref_refused"] is Status.PASSED
    assert statuses["signed.bundle_verifies"] is Status.PASSED
    assert statuses["signed.tamper_detected"] is Status.PASSED
    level, blockers = report.verdict()
    assert level == "signed", f"expected SIGNED, got {level}; blockers: {blockers}"


async def test_without_exercise_the_claim_is_blocked_not_failed(
    rig_url: tuple[str, Path],
) -> None:
    """No opt-in, no level claim: the lifecycle check reports unexercised."""
    url, _ = rig_url
    report = await run_suite(url)
    statuses = _by_id(report)
    assert statuses["core.lifecycle.exercise"] is Status.UNEXERCISED
    assert not any(s is Status.FAILED for s in statuses.values())
    level, blockers = report.verdict()
    assert level == "none"
    assert any("core.lifecycle.exercise" in b for b in blockers)


async def test_report_json_round_trips(rig_url: tuple[str, Path]) -> None:
    """The JSON report carries every check with its spec reference."""
    import json

    url, _ = rig_url
    report = await run_suite(url)
    payload = json.loads(report.to_json())
    assert payload["instrument"] == "ConformanceRig-1"
    assert {c["id"] for c in payload["checks"]} >= {
        "core.initialize.negotiates_0_4",
        "core.describe.units_mandatory",
        "signed.tamper_detected",
    }
    assert all(c["spec"].startswith("SPEC ") for c in payload["checks"])
