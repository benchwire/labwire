"""Tests for `labwire-ophyd annotate` and `labwire-ophyd check`."""

import subprocess
import sys
from pathlib import Path

from labwire.bridges.ophyd.annotations import load_annotations, resolve
from labwire.bridges.ophyd.introspect import introspect
from ophyd.sim import SynAxis


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "labwire.bridges.ophyd", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_annotate_emits_a_starter_file_with_gaps_marked(tmp_path: Path) -> None:
    out = tmp_path / "labwire-ophyd.yaml"
    proc = _run("annotate", "ophyd.sim:motor", "--output", str(out))
    assert proc.returncode == 0, proc.stderr
    text = out.read_text()
    assert "ophyd.sim.SynAxis" in text
    assert "TODO" in text  # every unresolved unit is marked, not invented
    assert "setpoint" in text


def test_the_generated_file_parses_back_and_names_what_is_missing(tmp_path: Path) -> None:
    out = tmp_path / "labwire-ophyd.yaml"
    assert _run("annotate", "ophyd.sim:motor", "--output", str(out)).returncode == 0
    annotations = load_annotations(out)  # a valid file, TODOs and all
    assert "ophyd.sim.SynAxis" in annotations.devices


def test_filling_in_the_todos_makes_the_device_resolve(tmp_path: Path) -> None:
    out = tmp_path / "labwire-ophyd.yaml"
    assert _run("annotate", "ophyd.sim:motor", "--output", str(out)).returncode == 0
    filled = out.read_text().replace("TODO-unit", "mm")
    out.write_text(filled)
    resolved = resolve(introspect(SynAxis(name="motor")), load_annotations(out))
    assert resolved.component("motor").unit == "mm"


def test_annotate_writes_to_stdout_without_an_output_path() -> None:
    proc = _run("annotate", "ophyd.sim:motor")
    assert proc.returncode == 0, proc.stderr
    assert "devices:" in proc.stdout


def test_annotate_reports_an_unimportable_target() -> None:
    proc = _run("annotate", "nosuchmodule:device")
    assert proc.returncode != 0
    assert "nosuchmodule" in proc.stderr


def test_annotate_rejects_a_malformed_target() -> None:
    proc = _run("annotate", "ophyd.sim.motor")  # missing the colon
    assert proc.returncode != 0
    assert "module:attribute" in proc.stderr


def test_check_fails_on_an_unannotated_device(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("version: 1\ndevices: {}\n")
    proc = _run("check", "ophyd.sim:motor", "--annotations", str(empty))
    assert proc.returncode == 1
    assert "unit" in proc.stderr


def test_check_passes_once_the_gaps_are_closed(tmp_path: Path) -> None:
    out = tmp_path / "labwire-ophyd.yaml"
    assert _run("annotate", "ophyd.sim:motor", "--output", str(out)).returncode == 0
    out.write_text(out.read_text().replace("TODO-unit", "mm"))
    proc = _run("check", "ophyd.sim:motor", "--annotations", str(out))
    assert proc.returncode == 0, proc.stderr
    # SynAxis: readback+setpoint are channels; velocity+acceleration are config
    assert "2 channel(s), 4 component(s)" in proc.stdout
    assert "S2  move" in proc.stdout


def test_check_allow_partial_reports_what_it_dropped(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("version: 1\ndevices: {}\n")
    proc = _run("check", "ophyd.sim:motor", "--annotations", str(empty), "--allow-partial")
    assert proc.returncode == 0, proc.stderr
    assert "omitted" in proc.stdout.lower()
