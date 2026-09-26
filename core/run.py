"""Run S0 → S2 for one recording: what notebook cell C4 calls (DEC-013).

Everything the notebook does lives here so it is unit-tested; the notebook
cells stay a few lines long.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core import s0_preflight, s1_stage_in, s2_probe
from core.config import ConfigError, load_config, resolve_paths
from core.contracts import MediaInfo
from core.s0_preflight import human_size

TARGET_FPS_CHOICES = ("config", "auto", "30", "60")


@dataclass(frozen=True)
class Result:
    match_id: str
    media_info: MediaInfo
    drive_media_info: Path
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


def run_to_probe(choice: str, target_fps: str = "config", force: bool = False,
                 config_path: Optional[Path] = None,
                 log: Callable[[str], None] = print) -> Result:
    """S0 → S1 → S2 for one inbox recording. Raises StageError with a plain-English message."""
    name = resolve_choice(choice, config_path)

    t0 = time.monotonic()
    log(f"S0 Preflight: checking Colab, Drive and '{name}' ...")
    pre = s0_preflight.run(name, overrides_for(target_fps), config_path)
    log(f"  {pre.ffmpeg_version}; {pre.cpu_count} CPUs; GPU: {pre.gpu or 'none'}"
        f" (GPU encoder {'works' if pre.gpu_encoder else 'not available'}); "
        f"{human_size(pre.free_disk_bytes)} free on local disk  [{time.monotonic() - t0:.1f} s]")

    t1 = time.monotonic()
    log(f"S1 Stage-in: copying {human_size(pre.source_size_bytes)} to local disk ...")
    shown = [0]

    def progress(fraction: float) -> None:
        step = int(fraction * 10)
        if step > shown[0]:
            shown[0] = step
            log(f"  {step * 10}%")

    stage_in = s1_stage_in.run(pre, force=force, progress=progress)
    log(f"  match_id {stage_in.match_id} (time from {stage_in.time_source}); "
        f"{'copied and size verified' if stage_in.copied else 'reused existing local copy (same size)'}"
        f"  [{time.monotonic() - t1:.1f} s]")

    t2 = time.monotonic()
    log("S2 Probe: measuring the recording ...")
    probe = s2_probe.run(pre, stage_in)
    log(summary(probe.media_info))
    log(f"  saved {probe.drive_path}  [{time.monotonic() - t2:.1f} s]")
    return Result(stage_in.match_id, probe.media_info, probe.drive_path,
                  list(probe.media_info.warnings))
