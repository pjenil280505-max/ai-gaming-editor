"""Test-media fixtures F1–F10 (docs/PHASE0.md §Test fixtures), made with FFmpeg.

Nothing here is committed media: every file is generated on first use into one
temp dir that is deleted when the Python process exits (CLAUDE.md rule 7).

Every fixture except F9 (corrupt) carries the timing references later tests
rely on: a full-frame white flash on the frame at t = 0, 5, 10, 15 ... s and a
100 ms 1 kHz beep starting at the same instants (F4 has no audio).
F5's second track instead beeps at 440 Hz half-way between flashes, so a test
can tell which track a master used.

Usage from tests:   path = fixtures.path("F1")
Stand-alone timing: python tests/fixtures.py [--slow]
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PERIOD_S = 5.0          # flash + beep every 5 s
BEEP_S = 0.1            # beep length
BEEP_HZ = 1000
BEEP_AMPLITUDE = 0.5


@dataclass(frozen=True)
class Track:
    """One audio track: beeps at `beep_hz` whenever (t_global - phase_s) is a multiple of 5 s."""
    sample_rate: int = 44100
    channels: int = 2
    beep_hz: int = BEEP_HZ
    phase_s: float = 0.0


@dataclass(frozen=True)
class Section:
    fps: int
    duration_s: float


@dataclass(frozen=True)
class Spec:
    description: str
    sections: tuple[Section, ...] = (Section(30, 20.0),)
    width: int = 320
    height: int = 180
    vcodec: str = "h264"
    tracks: tuple[Track, ...] = (Track(),)
    audio_offset_s: float = 0.0     # audio starts this long after video
    rotation_cw: int = 0            # classic 'rotate' tag convention
    truncated: bool = False
    slow: bool = False

    @property
    def duration_s(self) -> float:
        return sum(s.duration_s for s in self.sections)

    @property
    def frame_count(self) -> int:
        return sum(round(s.fps * s.duration_s) for s in self.sections)


SPECS: dict[str, Spec] = {
    "F1": Spec("constant 30 fps, 20 s, H.264 + AAC, 320x180"),
    "F2": Spec("variable frame rate: 60 fps 0–7 s, 45 fps 7–13 s, 30 fps 13–20 s",
               sections=(Section(60, 7.0), Section(45, 6.0), Section(30, 7.0))),
    "F3": Spec("rotation metadata 90° (clockwise)", rotation_cw=90),
    "F4": Spec("no audio", tracks=()),
    "F5": Spec("two audio tracks (0: stereo 44.1 kHz 1 kHz beeps; 1: mono 48 kHz 440 Hz beeps at +2.5 s)",
               tracks=(Track(), Track(sample_rate=48000, channels=1, beep_hz=440, phase_s=2.5))),
    "F6": Spec("HEVC video", vcodec="hevc"),
    "F7": Spec("audio starts 0.5 s after video", audio_offset_s=0.5),
    "F8": Spec("20:9 aspect, 800x360", width=800, height=360),
    "F9": Spec("truncated/corrupt file (first half of an F1-style file)", truncated=True),
    "F10": Spec("10 min at 160x90, 30 fps, for resume/timing tests",
                sections=(Section(30, 600.0),), width=160, height=90, slow=True),
}

FAST = tuple(name for name, spec in SPECS.items() if not spec.slow)
SLOW = tuple(name for name, spec in SPECS.items() if spec.slow)

# Seconds spent generating each fixture in this process (for reporting).
GENERATION_SECONDS: dict[str, float] = {}


class FixtureError(RuntimeError):
    pass


def slow_enabled() -> bool:
    return os.environ.get("RUN_SLOW") == "1"


# FFmpeg 6.1.1 on both Colab and the cloud box (DEC-009); no fallbacks for older builds.

def _run(cmd: list[str]) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise FixtureError("ffmpeg/ffprobe not found on PATH; see docs/cloud-setup.md")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FixtureError(f"command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr[-2000:]}")


# ---- filtergraph pieces

def _video_section(spec: Spec, section: Section, start_s: float) -> str:
    # Flash exactly one frame at every multiple of 5 s of global time.
    return (f"testsrc2=s={spec.width}x{spec.height}:r={section.fps}:d={section.duration_s},"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:"
            f"enable='lt(mod(t+{start_s}+0.0001,{PERIOD_S}),1/{section.fps})'")


def _audio_source(track: Track, start_s: float, duration_s: float) -> str:
    # Sample-exact beeps: local t=0 is global time start_s.
    shift = start_s - track.phase_s + PERIOD_S
    layout = "stereo" if track.channels == 2 else "mono"
    return (f"aevalsrc='if(lt(mod(t+{shift}+1e-7,{PERIOD_S}),{BEEP_S}),"
            f"{BEEP_AMPLITUDE}*sin(2*PI*{track.beep_hz}*t),0)'"
            f":s={track.sample_rate}:d={duration_s}:c={layout}")


def _video_codec_args(spec: Spec) -> list[str]:
    if spec.vcodec == "hevc":
        return ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
                "-tag:v", "hvc1", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]


def _encode(spec: Spec, out: Path) -> None:
    graph, start = [], 0.0
    for i, section in enumerate(spec.sections):
        graph.append(f"{_video_section(spec, section, start)}[v{i}]")
        start += section.duration_s
    n = len(spec.sections)
    if n > 1:
        graph.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]")
        video_label = "[v]"
    else:
        video_label = "[v0]"

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-filter_complex", ";".join(graph)]
    for track in spec.tracks:
        if spec.audio_offset_s:
            cmd += ["-itsoffset", str(spec.audio_offset_s)]
        cmd += ["-f", "lavfi", "-i",
                _audio_source(track, spec.audio_offset_s, spec.duration_s - spec.audio_offset_s)]

    cmd += ["-map", video_label]
    for i in range(len(spec.tracks)):
        cmd += ["-map", f"{i}:a"]
    cmd += _video_codec_args(spec)
    if n > 1:
        cmd += ["-fps_mode", "passthrough", "-enc_time_base:v", "1:90000"]
    if spec.tracks:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd.append(str(out))
    _run(cmd)


def _set_rotation(src: Path, out: Path, degrees_cw: int) -> None:
    # -display_rotation takes a counter-clockwise angle; -90 == classic rotate=90 tag
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
          "-display_rotation", str(-degrees_cw), "-i", str(src), "-c", "copy", str(out)])


def generate(name: str, directory: Path) -> Path:
    """Generate fixture `name` into `directory` and return its path."""
    spec = SPECS[name]
    out = directory / f"{name}.mp4"
    started = time.monotonic()
    if spec.rotation_cw or spec.truncated:
        whole = directory / f"{name}.source.mp4"
        _encode(spec, whole)
        if spec.rotation_cw:
            _set_rotation(whole, out, spec.rotation_cw)
        else:
            data = whole.read_bytes()
            out.write_bytes(data[: len(data) // 2])
        whole.unlink()
    else:
        _encode(spec, out)
    GENERATION_SECONDS[name] = time.monotonic() - started
    return out


# ---- process-wide cache

_cache_dir: Optional[Path] = None
_paths: dict[str, Path] = {}


def _directory() -> Path:
    global _cache_dir
    if _cache_dir is None:
        _cache_dir = Path(tempfile.mkdtemp(prefix="aieditor-fixtures-"))
        atexit.register(shutil.rmtree, _cache_dir, True)
    return _cache_dir


def path(name: str) -> Path:
    """Path to fixture `name`, generating it on first use."""
    if name not in SPECS:
        raise KeyError(f"unknown fixture {name!r}")
    if SPECS[name].slow and not slow_enabled():
        raise FixtureError(f"{name} is only generated when RUN_SLOW=1")
    if name not in _paths:
        _paths[name] = generate(name, _directory())
    return _paths[name]


if __name__ == "__main__":
    names = FAST + (SLOW if "--slow" in sys.argv[1:] else ())
    with tempfile.TemporaryDirectory(prefix="aieditor-fixtures-") as tmp:
        total = time.monotonic()
        for fixture in names:
            file = generate(fixture, Path(tmp))
            print(f"{fixture:4} {GENERATION_SECONDS[fixture]:6.2f} s  {file.stat().st_size:>10,} bytes  "
                  f"{SPECS[fixture].description}")
        print(f"total {time.monotonic() - total:.2f} s for {len(names)} fixtures")
