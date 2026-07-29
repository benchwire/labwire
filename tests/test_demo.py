"""CI smoke tests for the closed-loop demo (`make demo` / `make demo-claude`)."""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parents[1]


def _run_demo(target: str, tmp_path: Path, *, strip_key: bool) -> subprocess.CompletedProcess[str]:
    # invoked through make, so the README-documented entry points are what CI tests
    env = dict(os.environ)
    env["DEMO_FAST"] = "1"
    env["DEMO_RUNS_DIR"] = str(tmp_path / "runs")
    if strip_key:
        env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run(
        ["make", target],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        cwd=REPO,
        check=False,
    )


def test_scripted_demo_produces_verified_bundle(tmp_path: Path) -> None:
    proc = _run_demo("demo", tmp_path, strip_key=False)
    assert proc.returncode == 0, proc.stderr
    assert "converged: best yield" in proc.stdout
    assert "OK - authentic" in proc.stdout


def test_claude_demo_degrades_gracefully_without_key(tmp_path: Path) -> None:
    proc = _run_demo("demo-claude", tmp_path, strip_key=True)
    assert proc.returncode == 0, proc.stderr
    assert "ANTHROPIC_API_KEY not set" in proc.stdout
    assert "OK - authentic" in proc.stdout


def test_streaming_example_runs_clean(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO / "examples" / "streaming.py")],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "settled: halted (pump reports IDLE after STP" in proc.stdout
    assert "declares cancel_semantics 'none'" in proc.stdout
    assert "the slew finished anyway, honestly" in proc.stdout
    assert "after recovery" in proc.stdout
    assert "done" in proc.stdout


def test_ophyd_scan_demo_finds_a_peak_and_signs_it(tmp_path: Path) -> None:
    proc = _run_demo("demo-ophyd", tmp_path, strip_key=False)
    assert proc.returncode == 0, proc.stderr
    assert "peak found" in proc.stdout
    assert "S2       move" in proc.stdout  # safety classes are surfaced
    assert "OK - authentic" in proc.stdout


def test_ophyd_claude_scan_degrades_gracefully_without_key(tmp_path: Path) -> None:
    proc = _run_demo("demo-ophyd-claude", tmp_path, strip_key=True)
    assert proc.returncode == 0, proc.stderr
    assert "ANTHROPIC_API_KEY not set" in proc.stdout
    assert "OK - authentic" in proc.stdout


def test_pylabrobot_dilution_demo_runs_the_full_s3_ceremony(tmp_path: Path) -> None:
    proc = _run_demo("demo-pylabrobot", tmp_path, strip_key=False)
    assert proc.returncode == 0, proc.stderr
    assert "S2       transfer" in proc.stdout  # safety classes are surfaced
    assert "S3       move_plate" in proc.stdout
    assert "nominal 1:2" in proc.stdout
    # the four beats, in order: refused absent, operator approves, granted,
    # then the same valid grant refused on different parameters
    out = proc.stdout
    refused = out.index("REFUSED -32011  reason=absent")
    approved = out.index("labwire grant approve")
    granted = out.index("GRANTED")
    mismatch = out.index("reason=params_mismatch")
    assert refused < approved < granted < mismatch
    assert "OK - authentic" in out
    assert "identity_verified False" in out
    # a bearer grant id never lands in a signed bundle
    import json as _json

    for manifest_path in (tmp_path / "runs").glob("*/manifest.json"):
        assert "grant_id" not in _json.dumps(_json.loads(manifest_path.read_text()))


def test_pylabrobot_claude_dilution_degrades_gracefully_without_key(tmp_path: Path) -> None:
    proc = _run_demo("demo-pylabrobot-claude", tmp_path, strip_key=True)
    assert proc.returncode == 0, proc.stderr
    assert "ANTHROPIC_API_KEY not set" in proc.stdout
    # the scripted fallback exercises the same ceremony CI asserts above
    assert "reason=absent" in proc.stdout
    assert "reason=params_mismatch" in proc.stdout
    assert "OK - authentic" in proc.stdout
