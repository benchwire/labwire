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
    assert "canceled cleanly" in proc.stdout
    assert "after recovery" in proc.stdout
    assert "done" in proc.stdout
