"""Independent media measurements for tests, straight from ffprobe/ffmpeg.

Deliberately separate from core/: tests use these to check fixtures now and
pipeline outputs later, so they must not share code with what they check.
"""

from __future__ import annotations

import array
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Optional


class MeasureError(RuntimeError):
    pass


def ffprobe(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True)


def probe(path: Path) -> dict:
    """ffprobe format + streams as a dict; raises MeasureError if ffprobe fails."""
    result = ffprobe(path)
    if result.returncode != 0:
        raise MeasureError(f"ffprobe failed on {path}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def streams(info: dict, codec_type: str) -> list[dict]:
    return [s for s in info["streams"] if s["codec_type"] == codec_type]


def rotation_cw(stream: dict) -> int:
    """Clockwise display rotation from the display matrix (0 when there is none)."""
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            return int(round(-float(side["rotation"]))) % 360
    return 0


def video_frame_durations(path: Path) -> list[float]:
    """Durations between consecutive video packets in presentation order, seconds."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=time_base:packet=pts", "-of", "json", str(path)],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise MeasureError(result.stderr.strip())
    data = json.loads(result.stdout)
    time_base = Fraction(data["streams"][0]["time_base"])
    pts = sorted(int(p["pts"]) for p in data["packets"] if "pts" in p)
    return [float((b - a) * time_base) for a, b in zip(pts, pts[1:])]


def flash_times(path: Path, min_luma: float = 200.0) -> list[float]:
    """Presentation times of frames whose average luma is at least `min_luma` (white flashes)."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
         "-map", "0:v:0",
         "-vf", "signalstats,metadata=mode=print:key=lavfi.signalstats.YAVG:file=-",
         "-f", "null", "-"],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise MeasureError(result.stderr.strip())
    times, current = [], None
    for line in result.stdout.splitlines():
        if line.startswith("frame:"):
            current = float(line.split("pts_time:")[1].split()[0])
        elif line.startswith("lavfi.signalstats.YAVG=") and current is not None:
            if float(line.split("=", 1)[1]) >= min_luma:
                times.append(current)
    return times


class AudioTrack:
    """One decoded audio stream (mono mix) placed on the file's timeline."""

    def __init__(self, path: Path, audio_index: int):
        info = probe(path)
        stream = streams(info, "audio")[audio_index]
        self.sample_rate = int(stream["sample_rate"])
        self.start_s = float(stream.get("start_time", 0.0))
        raw = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
             "-map", f"0:a:{audio_index}", "-ac", "1", "-f", "s16le", "-acodec", "pcm_s16le", "-"],
            capture_output=True)
        if raw.returncode != 0:
            raise MeasureError(raw.stderr.decode(errors="replace").strip())
        self.samples = array.array("h")
        self.samples.frombytes(raw.stdout)
        if sys.byteorder == "big":
            self.samples.byteswap()
        self.peak = max(max(self.samples, default=0), -min(self.samples, default=0))

    def _index(self, t: float) -> int:
        return min(max(int(round((t - self.start_s) * self.sample_rate)), 0), len(self.samples))

    def onset(self, expected_s: float, window_s: float = 0.05) -> Optional[float]:
        """First time within ±window of `expected_s` where the level passes 30 % of peak."""
        threshold = 0.3 * self.peak
        lo, hi = self._index(expected_s - window_s), self._index(expected_s + window_s)
        for i in range(lo, hi):
            if abs(self.samples[i]) > threshold:
                return self.start_s + i / self.sample_rate
        return None

    def max_level(self, t0: float, t1: float) -> int:
        chunk = self.samples[self._index(t0):self._index(t1)]
        return max(max(chunk, default=0), -min(chunk, default=0))

    def frequency(self, t0: float, t1: float) -> float:
        """Tone frequency between t0 and t1 from zero crossings."""
        chunk = self.samples[self._index(t0):self._index(t1)]
        crossings = sum(1 for a, b in zip(chunk, chunk[1:]) if (a < 0) != (b < 0))
        return crossings / 2 / (len(chunk) / self.sample_rate)
