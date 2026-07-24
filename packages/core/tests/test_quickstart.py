"""The 5-minute-stranger guard: examples/quickstart.py must actually run."""

import subprocess
import sys
from pathlib import Path

QUICKSTART = Path(__file__).parents[3] / "examples" / "quickstart.py"


def test_quickstart_exists() -> None:
    assert QUICKSTART.is_file(), f"missing {QUICKSTART}"


def test_quickstart_runs_and_shows_a_result() -> None:
    proc = subprocess.run(
        [sys.executable, str(QUICKSTART)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "succeeded" in proc.stdout
    assert "mass_g" in proc.stdout
