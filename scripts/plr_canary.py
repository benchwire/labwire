"""Compatibility canary: the PyLabRobot bridge against PLR's v1b1 branch.

PyLabRobot is being redesigned on its ``v1b1`` branch (devices become
plain classes; the LiquidHandler world moves under ``pylabrobot.legacy``
behind deprecation shims). The published ``labwire-pylabrobot`` pins
``pylabrobot>=0.2,<0.3`` so released users never see that branch; this
script exists so WE see it early. It is run by the scheduled
``plr-canary`` workflow after installing PLR straight from the branch
tip, and it answers three questions in one markdown report:

1. Which imports still resolve: the bridge package itself, the single
   PLR import the bridge source makes, and every PLR import the test
   suite makes (old paths kept alive by shims are probed as old paths,
   deliberately, so we hear the DeprecationWarning and, later, the
   removal).
2. Does the shipped bridge test suite pass against the branch?
3. What exactly failed, in a form readable without downloading logs.

Exit code 0 means fully compatible today; 1 means drift (the workflow
is allowed to fail; red is the early-warning signal, not a build
break). The report goes to stdout and, when ``GITHUB_STEP_SUMMARY`` is
set, to the job summary.
"""

import importlib.metadata
import json
import os
import pathlib
import re
import subprocess
import sys
import warnings

BRIDGE = "packages/bridges/pylabrobot"

# The import contract, from the bridge-assumptions inventory: the first
# entry is the bridge package itself (must import with or without PLR),
# the second is the only PLR import in bridge SOURCE, the rest are what
# the TEST SUITE imports (old shim paths on purpose; see module doc).
PROBES = [
    ("labwire.bridges.pylabrobot", None),
    ("pylabrobot.resources", "set_tip_tracking"),
    ("pylabrobot.liquid_handling", "LiquidHandler"),
    ("pylabrobot.liquid_handling.backends", "LiquidHandlerChatterboxBackend"),
    ("pylabrobot.liquid_handling.errors", "ChannelizedError"),
    ("pylabrobot.resources", "Cor_96_wellplate_360ul_Fb"),
    ("pylabrobot.resources", "PLT_CAR_L5AC_A00"),
    ("pylabrobot.resources.hamilton", "STARLetDeck"),
    ("pylabrobot.resources.hamilton", "hamilton_96_tiprack_1000uL_filter"),
]


def installed_plr() -> str:
    """The installed PLR version and, for a VCS install, its commit."""
    version = importlib.metadata.version("pylabrobot")
    commit = "unknown commit"
    dist = importlib.metadata.distribution("pylabrobot")
    for f in dist.files or []:
        if f.name == "direct_url.json":
            data = json.loads(pathlib.Path(dist.locate_file(f)).read_text())
            commit = data.get("vcs_info", {}).get("commit_id", commit)
    return f"pylabrobot {version} @ {commit}"


def probe_imports() -> tuple[list[str], int]:
    """Probe every import the bridge and its tests rely on."""
    lines: list[str] = []
    failures = 0
    for module, symbol in PROBES:
        target = f"{module}.{symbol}" if symbol else module
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                mod = __import__(module, fromlist=[symbol] if symbol else [])
                if symbol is not None:
                    getattr(mod, symbol)
            except Exception as exc:  # the report IS the error handler
                failures += 1
                lines.append(f"| `{target}` | FAILED | {type(exc).__name__}: {exc} |")
                continue
        notes = "; ".join(
            str(w.message)[:80] for w in caught if issubclass(w.category, DeprecationWarning)
        )
        lines.append(f"| `{target}` | ok | {notes or ''} |")
    return lines, failures


def run_suite() -> tuple[str, int, str]:
    """Run the bridge suite; return (summary line, returncode, output tail)."""
    proc = subprocess.run(
        # No -q here: root pyproject addopts already has -q, and doubling
        # it (-qq) suppresses the final counts line the report parses.
        [sys.executable, "-m", "pytest", BRIDGE, "-W", "ignore"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    tail = "\n".join(output.strip().splitlines()[-25:])
    summary = "no summary line found"
    for line in reversed(output.splitlines()):
        if re.search(r"\d+ (passed|failed|error)", line):
            summary = line.strip()
            break
    return summary, proc.returncode, tail


def main() -> int:
    """Build and emit the report; exit 1 on any drift."""
    report: list[str] = ["# PLR v1b1 canary", "", f"Installed: {installed_plr()}", ""]
    report.append("## Import contract")
    report.append("")
    report.append("| import | status | notes (deprecations) |")
    report.append("|---|---|---|")
    import_lines, import_failures = probe_imports()
    report.extend(import_lines)
    report.append("")
    report.append("## Bridge test suite")
    report.append("")
    suite_summary, suite_rc, tail = run_suite()
    report.append(f"Result: `{suite_summary}`")
    if suite_rc != 0:
        report.append("")
        report.append("<details><summary>pytest tail</summary>")
        report.append("")
        report.append("```")
        report.append(tail)
        report.append("```")
        report.append("</details>")
    report.append("")
    drift = import_failures > 0 or suite_rc != 0
    verdict = (
        "DRIFT: the bridge is not fully compatible with today's v1b1 (expected while "
        "the redesign is in flight; red here never gates normal CI)."
        if drift
        else "COMPATIBLE: shipped bridge fully green against today's v1b1."
    )
    report.append(f"**{verdict}**")
    text = "\n".join(report)
    sys.stdout.write(text + "\n")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
