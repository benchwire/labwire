"""Retime the raw vhs recording of the live agent demo.

vhs records `make demo-pylabrobot-claude` in real time (docs/demo-agent.tape),
which includes whatever API latency and rate-limit backoff the live run hit.
This script rebuilds the recording with every content beat intact but idle
gaps capped, so the published clip stays under 45 seconds without touching
what was actually on screen. Nothing is reordered, redrawn, or synthesized:
the frames are the recording's own, only their display durations change.

Pipeline: explode the GIF to frames, drop consecutive duplicates, collapse
cursor-blink alternations (two screens differing only in the cursor cell)
into one held frame, cap each hold at CAP seconds, hold the final screen for
HOLD seconds, then re-encode. Needs ffmpeg; uses gifsicle for the final
palette squeeze when available.

Usage: python3 docs/demo-agent-retime.py [raw.gif]
Writes docs/demo-agent.gif and docs/demo-agent.mp4.
"""

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CAP = 2.5
HOLD = 5.0
DOCS = Path(__file__).resolve().parent


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def gif_duration(gif: Path) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(gif)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(probe.stdout.strip())


def distinct_runs(frames: "list[Path]", dt: float) -> "list[list]":
    """Consecutive identical frames become one [hash, path, seconds] run."""
    runs: list[list] = []
    prev = None
    for frame in frames:
        digest = hashlib.md5(frame.read_bytes()).hexdigest()
        if digest != prev:
            runs.append([digest, frame, 0.0])
            prev = digest
        runs[-1][2] += dt
    return runs


def collapse_blinks(runs: "list[list]") -> "list[list]":
    """A window alternating between exactly two screens is a blinking cursor."""
    events: list[list] = []
    i = 0
    while i < len(runs):
        j = i
        pair = {runs[i][0]}
        while j + 1 < len(runs) and (runs[j + 1][0] in pair or len(pair) < 2):
            nxt = runs[j + 1][0]
            if nxt not in pair:
                if j + 2 < len(runs) and runs[j + 2][0] in pair:
                    pair.add(nxt)
                else:
                    break
            j += 1
        if j - i >= 2:
            events.append([runs[i][1], sum(r[2] for r in runs[i : j + 1])])
            i = j + 1
        else:
            events.append([runs[i][1], runs[i][2]])
            i += 1
    return events


def main() -> None:
    raw = Path(sys.argv[1]) if len(sys.argv) > 1 else DOCS / "demo-agent.gif"
    total = gif_duration(raw)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        ffmpeg("-i", str(raw), "-vsync", "0", str(work / "f%05d.png"))
        frames = sorted(work.glob("f*.png"))
        events = collapse_blinks(distinct_runs(frames, total / max(len(frames) - 1, 1)))

        concat = work / "concat.txt"
        lines = ["ffconcat version 1.0"]
        for index, (frame, duration) in enumerate(events):
            held = HOLD if index == len(events) - 1 else min(duration, CAP)
            lines.append(f"file '{frame}'\nduration {held:.4f}")
        lines.append(f"file '{events[-1][0]}'")
        concat.write_text("\n".join(lines) + "\n")

        master = work / "master.gif"
        ffmpeg(
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-vsync",
            "vfr",
            "-filter_complex",
            "[0:v]split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=none",
            str(master),
        )
        ffmpeg(
            "-i",
            str(master),
            "-movflags",
            "faststart",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            str(DOCS / "demo-agent.mp4"),
        )
        if shutil.which("gifsicle"):
            subprocess.run(
                [
                    "gifsicle",
                    "-O3",
                    "--colors",
                    "16",
                    str(master),
                    "-o",
                    str(DOCS / "demo-agent.gif"),
                ],
                check=True,
            )
        else:
            shutil.copyfile(master, DOCS / "demo-agent.gif")
    out = DOCS / "demo-agent.gif"
    print(f"{len(events)} screens, {gif_duration(out):.1f}s -> {out} and demo-agent.mp4")


if __name__ == "__main__":
    main()
