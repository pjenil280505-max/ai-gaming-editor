"""S5 QC v0 (docs/PHASE0.md): checks the master and the analysis copy. Details in DEC-028.

Six checks, each pass / warn / fail. The results are saved to Drive as qc.json
and copied into run_report.json on every run, including runs that skip S5.
A failed check fails the stage, so it writes no marker and the match does not
count as done; a warning is something for the owner to look at.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core import ffmpeg, files
from core.config import get
from core.contracts import ContractError, MediaInfo, QcCheck, QcResults
from core.errors import StageError
from core.s0_preflight import Preflight
from core.s1_stage_in import StageIn
from core.s3_master import check_cfr

TITLE = "S5 QC"
FILE_NAME = "qc.json"
SCAN_LOG = "s5_qc.scan.ffmpeg.log"
LOUDNESS_LOG = "s5_qc.loudness.ffmpeg.log"

SPAN_MIN_S = 2.0            # shortest black or frozen span listed (FFmpeg's default)
BLACK_PIXEL = 0.10          # blackdetect pix_th: a pixel at most this bright (0–1) is black
BLACK_PICTURE = 0.98        # blackdetect pic_th: share of black pixels that makes a black picture
FREEZE_NOISE_DB = -60       # freezedetect n: frames closer than this count as the same picture
AUDIO_BLOCK_SAMPLES = 1024  # one AAC block: a recording's sound can only end on a block boundary
SCAN_SHARE = 0.5            # share of S5's progress bar for the black/frozen scan

LABELS = {
    "constant_frame_rate": "Constant frame rate",
    "length_vs_recording": "Length vs recording",
    "audio_vs_video_length": "Audio vs video length",
    "black_spans": "Black spans",
    "frozen_spans": "Frozen spans",
    "loudness": "Loudness",
}

Progress = Optional[Callable[[float], None]]


@dataclass(frozen=True)
class Qc:
    checks: list[QcCheck]
    drive_path: Path
    warnings: list[str]         # one line per check that warns
    failures: list[str]         # one line per check that fails


# ---- the checks


def check_frame_rate(master: Path, fps: int) -> QcCheck:
    problems = check_cfr(master, fps)
    return QcCheck("constant_frame_rate", "fail" if problems else "pass",
                   "; ".join(problems) or f"every frame lasts 1/{fps} s", f"1/{fps} s")


def check_length(master_s: float, recording_s: float, tolerance_s: float) -> QcCheck:
    diff = round(master_s - recording_s, 3)
    return QcCheck("length_vs_recording", "pass" if abs(diff) <= tolerance_s else "fail",
                   diff, tolerance_s)


def _stream_end(stream: dict) -> float:
    return float(stream.get("start_time") or 0) + float(stream["duration"])


def check_audio_vs_video(streams: list[dict], fps: int, tolerance_frames: float,
                         recording_rate: Optional[int]) -> QcCheck:
    """Audio end minus video end; allowed: the configured frames plus one audio block.

    The recording's last audio block is decoded whole, so the master's sound can
    run up to one block (21–23 ms) past the recording's real end; at 60 fps that
    alone is more than a frame (DEC-022, DEC-028).
    """
    video = next(s for s in streams if s.get("codec_type") == "video")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None or not recording_rate:
        return QcCheck("audio_vs_video_length", "warn", None, None)
    diff = _stream_end(audio) - _stream_end(video)
    allowed = tolerance_frames / fps + AUDIO_BLOCK_SAMPLES / recording_rate
    return QcCheck("audio_vs_video_length", "pass" if abs(diff) <= allowed + 1e-6 else "warn",
                   round(diff, 3), round(allowed, 4))


def check_spans(name: str, spans: list[dict]) -> QcCheck:
    return QcCheck(name, "warn" if spans else "pass", spans, SPAN_MIN_S)


def check_loudness(measured: Optional[dict]) -> QcCheck:
    return QcCheck("loudness", "pass" if measured else "warn", measured, None)


# ---- FFmpeg measurements


def scan_command(video: Path) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin", "-i", str(video),
            "-map", "0:v:0",
            "-vf", f"blackdetect=d={SPAN_MIN_S}:pix_th={BLACK_PIXEL}:pic_th={BLACK_PICTURE},"
                   f"freezedetect=n={FREEZE_NOISE_DB}dB:d={SPAN_MIN_S}",
            "-f", "null", "-"]


def _span(start: float, end: float, duration_s: float, frame_s: float) -> dict:
    # blackdetect ends a span that reaches the end at the last frame's start time
    if duration_s - end <= frame_s + 1e-6:
        end = duration_s
    return {"start_s": round(start, 2), "end_s": round(end, 2)}


def parse_spans(log: str, duration_s: float, frame_s: float) -> tuple[list[dict], list[dict]]:
    """Black and frozen spans from blackdetect/freezedetect log lines."""
    black = [_span(float(a), float(b), duration_s, frame_s) for a, b in
             re.findall(r"black_start:\s*([-\d.]+)\s+black_end:\s*([-\d.]+)", log)]
    frozen, start = [], None
    for key, value in re.findall(r"lavfi\.freezedetect\.freeze_(start|end):\s*([-\d.]+)", log):
        if key == "start":
            start = float(value)
        elif start is not None:
            frozen.append(_span(start, float(value), duration_s, frame_s))
            start = None
    if start is not None:           # frozen until the end: freezedetect reports no end
        frozen.append(_span(start, duration_s, duration_s, frame_s))
    return black, frozen


def loudness_command(master: Path) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin", "-i", str(master),
            "-map", "0:a:0", "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"]


def _finite(text: str) -> Optional[float]:
    value = float(text)
    return round(value, 1) if math.isfinite(value) else None


def parse_loudness(log: str) -> Optional[dict]:
    """Integrated loudness, true peak and loudness range from ebur128's summary; None if absent."""
    summary = log.rpartition("Summary:")[2]
    found = {}
    for key, pattern in (("integrated_lufs", r"\bI:\s+(\S+) LUFS"),
                         ("true_peak_dbtp", r"\bPeak:\s+(\S+) dBFS"),
                         ("lra_lu", r"\bLRA:\s+(\S+) LU\b")):
        match = re.search(pattern, summary)
        if not match:
            return None
        try:
            found[key] = _finite(match.group(1))
        except ValueError:
            return None
    return found


def _part(progress: Progress, start: float, end: float) -> Progress:
    """Map one pass's 0–1 progress onto [start, end] of the stage's progress."""
    if progress is None:
        return None
    return lambda fraction: progress(start + fraction * (end - start))


# ---- text for the owner


def _clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def describe(check: QcCheck) -> str:
    """One plain-English line for a check (summary.txt and C5's output)."""
    v, t = check.value, check.threshold
    if check.check == "constant_frame_rate":
        return str(v)
    if check.check == "length_vs_recording":
        if v == 0:
            return f"same length as the recording (limit {t:.3f} s)"
        return (f"master is {abs(v):.3f} s {'longer' if v > 0 else 'shorter'} than the "
                f"recording (limit {t:.3f} s)")
    if check.check == "audio_vs_video_length":
        if v is None:
            return "no sound, so this can't be checked"
        return (f"sound ends {abs(v) * 1000:.0f} ms {'after' if v >= 0 else 'before'} the "
                f"picture (limit {t * 1000:.0f} ms)")
    if check.check in ("black_spans", "frozen_spans"):
        if not v:
            return f"none of {t:g} s or longer"
        return ", ".join(f"{_clock(s['start_s'])}–{_clock(s['end_s'])} "
                         f"({s['end_s'] - s['start_s']:.1f} s)" for s in v)
    if check.check == "loudness":
        if v is None:
            return "no sound, nothing to measure"
        peak = ("silent" if v["true_peak_dbtp"] is None
                else f"{v['true_peak_dbtp']:.1f} dBTP true peak")
        lufs = ("silent" if v["integrated_lufs"] is None
                else f"{v['integrated_lufs']:.1f} LUFS integrated")
        lra = "no range" if v["lra_lu"] is None else f"{v['lra_lu']:.1f} LU range"
        return f"{lufs}, {peak}, {lra} (measured only, not applied)"
    return f"{v}"


def lines(checks: list[QcCheck]) -> list[str]:
    return [f"  {LABELS.get(c.check, c.check):<22} {c.status:<5} {describe(c)}" for c in checks]


# ---- the stage


def load(drive_dir: Path) -> Optional[list[QcCheck]]:
    """Results saved by an earlier S5 run; None if qc.json is missing or unreadable."""
    try:
        return QcResults.from_json((drive_dir / FILE_NAME).read_text(encoding="utf-8")).checks
    except (ContractError, OSError):
        return None


def outcome(checks: list[QcCheck]) -> tuple[list[str], list[str]]:
    """(warnings, failures) as plain-English lines."""
    def line(c):
        return f"{LABELS.get(c.check, c.check)}: {describe(c)}"
    warnings = [line(c) for c in checks if c.status == "warn"]
    failures = [line(c) + ". Send this message." for c in checks if c.status == "fail"]
    return warnings, failures


def run(pre: Preflight, stage_in: StageIn, media_info: MediaInfo, master: Path,
        analysis_video: Path, progress: Progress = None) -> Qc:
    """Run every check on the local master and analysis copy, then save qc.json to Drive."""
    config = pre.config
    fps = media_info.target_fps
    logs = stage_in.local_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    info = ffmpeg.probe(master)
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    track = next((t for t in media_info.audio_tracks if t.index == media_info.audio_track_index), None)

    analysis_s = float(ffmpeg.probe(analysis_video)["format"]["duration"])
    try:
        ffmpeg.run_with_progress(scan_command(analysis_video), analysis_s,
                                 _part(progress, 0.0, SCAN_SHARE if has_audio else 1.0),
                                 logs / SCAN_LOG)
        measured = None
        if has_audio:
            ffmpeg.run_with_progress(loudness_command(master), float(info["format"]["duration"]),
                                     _part(progress, SCAN_SHARE, 1.0), logs / LOUDNESS_LOG)
            measured = parse_loudness((logs / LOUDNESS_LOG).read_text(errors="replace"))
            if measured is None:
                raise ffmpeg.ToolError("no loudness summary in its log")
    except ffmpeg.ToolError as exc:
        raise StageError(TITLE, [f"FFmpeg could not check the master ({exc}). Re-run; if it "
                                 f"fails again, send this message."]) from None
    black, frozen = parse_spans((logs / SCAN_LOG).read_text(errors="replace"), analysis_s,
                                1 / get(config, "analysis.fps"))

    checks = [
        check_frame_rate(master, fps),
        check_length(float(info["format"]["duration"]), media_info.duration_s,
                     get(config, "qc.duration_tolerance_s")),
        check_audio_vs_video(info["streams"], fps, get(config, "qc.av_sync_tolerance_frames"),
                             track.sample_rate if track and has_audio else None),
        check_spans("black_spans", black),
        check_spans("frozen_spans", frozen),
        check_loudness(measured),
    ]
    results = QcResults(checks=checks)
    problems = results.validate()
    if problems:
        raise ValueError(f"QC results failed their contract: {problems}")

    drive_path = pre.paths.work / stage_in.match_id / FILE_NAME
    try:
        files.write_text(drive_path, results.to_json())
    except OSError as exc:
        raise StageError(TITLE, [f"Could not save {FILE_NAME} to Drive ({drive_path}): "
                                 f"{exc.strerror or exc}. Check Drive has space, then re-run."]) from None
    warnings, failures = outcome(checks)
    return Qc(checks, drive_path, warnings, failures)
