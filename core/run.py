"""Run the pipeline for one recording: what notebook cells C4 (choose) and C5 (run) call.

Everything the notebook does lives here so it is unit-tested; the notebook
cells stay a few lines long (DEC-013, DEC-024).
"""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core import s0_preflight, s1_stage_in, s2_probe, s3_master, s4_analysis
from core.config import ConfigError, get, load_config, resolve_paths
from core.contracts import MediaInfo
from core.s0_preflight import human_size
from core.s3_master import Master
from core.s4_analysis import Analysis

TARGET_FPS_CHOICES = ("config", "auto", "30", "60")
D1_LIMIT = 1.5          # docs/PHASE0.md D1: S3 slower than this x real time changes Phase 5

log_default = functools.partial(print, flush=True)


@dataclass(frozen=True)
class Result:
    match_id: str
    media_info: MediaInfo
    drive_dir: Path
    master: Master
    analysis: Analysis
    warnings: list[str]


def inbox_files(inbox: Path) -> list[Path]:
    """Recordings in the inbox, newest first (hidden files and folders skipped)."""
    files = [p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")]
    return sorted(files, key=lambda p: (-p.stat().st_mtime, p.name))


def inbox_listing(config_path: Optional[Path] = None) -> str:
    """Numbered inbox listing for the C4 form, or a plain-English reason it can't be listed."""
    try:
        paths = resolve_paths(load_config(config_path) if config_path else load_config())
    except ConfigError as exc:
        return str(exc)
    if not paths.inbox.is_dir():
        return f"Inbox folder {paths.inbox} not found. Run cell C1 and check the folder exists."
    files = inbox_files(paths.inbox)
    if not files:
        return f"The inbox {paths.inbox} is empty. Upload a recording there first."
    lines = [f"Recordings in {paths.inbox} (newest first):"]
    lines += [f"  {i}. {p.name}  ({human_size(p.stat().st_size)})" for i, p in enumerate(files, 1)]
    lines.append("Type the number or the file name into the 'recording' field and run this cell again.")
    return "\n".join(lines)


def resolve_choice(choice: str, config_path: Optional[Path] = None) -> str:
    """Turn the form's 'recording' field (a list number or a file name) into a file name."""
    choice = choice.strip()
    if not choice.isdigit():
        return choice
    try:
        inbox = resolve_paths(load_config(config_path) if config_path else load_config()).inbox
        files = inbox_files(inbox) if inbox.is_dir() else []
    except ConfigError:
        files = []
    number = int(choice)
    if 1 <= number <= len(files):
        return files[number - 1].name
    return choice      # S0 reports it as not found


def overrides_for(target_fps: str) -> dict:
    if target_fps not in TARGET_FPS_CHOICES:
        raise ValueError(f"target_fps must be one of {TARGET_FPS_CHOICES}")
    if target_fps == "config":
        return {}
    return {"video.target_fps": target_fps if target_fps == "auto" else int(target_fps)}


def summary(info: MediaInfo) -> str:
    v = info.video
    audio = ", ".join(f"track {t.index}: {t.codec} {t.sample_rate} Hz {t.channels} ch"
                      for t in info.audio_tracks) or "none"
    lines = [
        f"  container {info.container}, {info.duration_s:.1f} s, {human_size(info.size_bytes)}",
        f"  video {v.codec} {v.width}x{v.height}, rotation {v.rotation}°, shown as {v.display_aspect}",
        f"  frame rate {'VARIABLE' if v.is_vfr else 'constant'}: {v.vfr_evidence}",
        f"  master will be {info.target_fps} fps",
        f"  audio {audio}; using "
        + ("none (video-only)" if info.audio_track_index is None else f"track {info.audio_track_index}"),
    ]
    lines += [f"  WARNING: {w}" for w in info.warnings]
    return "\n".join(lines)


def describe_choice(choice: str, config_path: Optional[Path] = None) -> str:
    """C4's answer once a recording is typed in: what was chosen, or why it can't be used."""
    name = resolve_choice(choice, config_path)
    try:
        inbox = resolve_paths(load_config(config_path) if config_path else load_config()).inbox
    except ConfigError as exc:
        return str(exc)
    if name and Path(name).name == name and (inbox / name).is_file():
        return (f"Selected: {name} ({human_size((inbox / name).stat().st_size)}).\n"
                f"Now run cell C5 to process it.")
    return f"'{choice}' is not a recording in the inbox.\n" + inbox_listing(config_path)


def clock(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


class Steps:
    """Calls log once each time progress passes another `step` percent."""

    def __init__(self, log: Callable[[str], None], step: int,
                 describe: Callable[[int, float], str] = lambda pct, _: f"  {pct}%"):
        self.log, self.step, self.describe, self.shown = log, step, describe, 0

    def __call__(self, fraction: float) -> None:
        pct = int(fraction * 100) // self.step * self.step
        if pct > self.shown:
            self.shown = pct
            self.log(self.describe(pct, fraction))


def media_steps(log: Callable[[str], None], duration_s: float, step: int) -> Steps:
    """Progress lines with position, speed (x real time) and time left, for encodes."""
    started = time.monotonic()

    def describe(pct: int, fraction: float) -> str:
        done = fraction * duration_s
        text = f"  {pct}% · {clock(done)} of {clock(duration_s)}"
        if done > 0:
            ratio = (time.monotonic() - started) / done
            text += f" · {ratio:.2f}x real time · about {clock((duration_s - done) * ratio)} left"
        return text

    return Steps(log, step, describe)


def run_pipeline(choice: str, target_fps: str = "config", force: bool = False,
                 config_path: Optional[Path] = None,
                 log: Callable[[str], None] = log_default) -> Result:
    """S0 → S4 for one inbox recording. Raises StageError with a plain-English message."""
    name = resolve_choice(choice, config_path)

    t0 = time.monotonic()
    log(f"S0 Preflight: checking Colab, Drive and '{name}' ...")
    pre = s0_preflight.run(name, overrides_for(target_fps), config_path)
    log(f"  {pre.ffmpeg_version}; {pre.cpu_count} CPUs; GPU: {pre.gpu or 'none'}"
        f" (GPU encoder {'works' if pre.gpu_encoder else 'not available'}); "
        f"{human_size(pre.free_disk_bytes)} free on local disk  [{time.monotonic() - t0:.1f} s]")

    t1 = time.monotonic()
    log(f"S1 Stage-in: copying {human_size(pre.source_size_bytes)} to local disk ...")
    stage_in = s1_stage_in.run(pre, force=force, progress=Steps(log, 10))
    log(f"  match_id {stage_in.match_id} (time from {stage_in.time_source}); "
        f"{'copied and size verified' if stage_in.copied else 'reused existing local copy (same size)'}"
        f"  [{time.monotonic() - t1:.1f} s]")

    t2 = time.monotonic()
    log("S2 Probe: measuring the recording ...")
    probe = s2_probe.run(pre, stage_in)
    info = probe.media_info
    log(summary(info))
    log(f"  saved {probe.drive_path}  [{time.monotonic() - t2:.1f} s]")

    log(f"S3 Master: encoding {clock(info.duration_s)} at {info.target_fps} fps "
        f"(the slow step; progress every 5%) ...")
    master = s3_master.run(pre, stage_in, probe, progress=media_steps(log, info.duration_s, 5),
                           copy_progress=Steps(log, 25, lambda pct, _: f"  copied to Drive {pct}%"))
    ratio = master.encode_seconds / master.duration_s if master.duration_s else 0.0
    log(f"  master {master.width}x{master.height}, {master.fps} fps, {clock(master.duration_s)}, "
        f"{human_size(master.size_bytes)}; frame timing checked")
    log(f"  encoding took {clock(master.encode_seconds)} = {ratio:.2f}x real time "
        f"({'within' if ratio <= D1_LIMIT else 'OVER'} the D1 limit of {D1_LIMIT}x)")
    log(f"  saved {master.drive_path}")

    t4 = time.monotonic()
    log("S4 Analysis copy: making the small video and WAV ...")
    analysis = s4_analysis.run(pre, stage_in, master, progress=Steps(log, 25))
    wav = (f"; analysis.wav {get(pre.config, 'analysis.audio_channels')} ch "
           f"{get(pre.config, 'analysis.audio_rate')} Hz" if analysis.local_audio else "")
    log(f"  analysis.mp4 {analysis.width}x{analysis.height}, {get(pre.config, 'analysis.fps')} fps"
        f"{wav}  [{time.monotonic() - t4:.1f} s]")

    warnings = list(info.warnings) + master.warnings + analysis.warnings
    for w in master.warnings + analysis.warnings:
        log(f"  WARNING: {w}")
    drive_dir = master.drive_path.parent
    log(f"\nDone. Files for match {stage_in.match_id} are in {drive_dir}:\n  "
        + ", ".join(sorted(p.name for p in drive_dir.iterdir() if not p.name.endswith(".partial"))))
    return Result(stage_in.match_id, info, drive_dir, master, analysis, warnings)
