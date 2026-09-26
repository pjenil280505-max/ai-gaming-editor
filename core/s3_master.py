"""S3 Master (docs/PHASE0.md): constant-frame-rate master with rotation applied.

Timing comes from each frame's real timestamp: frames are repeated or dropped
to land on the target_fps grid, never re-timed by count, so picture and sound
stay together (clip 1: re-timing by count would drift 6.9 s). Audio keeps its
original levels (DEC-002). Details in DEC-022.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

from core import ffmpeg, files
from core.config import get
from core.contracts import MediaInfo
from core.errors import StageError
from core.s0_preflight import Preflight
from core.s1_stage_in import StageIn
from core.s2_probe import Probe, frame_durations

TITLE = "S3 Master"
FILE_NAME = "master.mp4"
AUDIO_BITRATE = "192k"

Progress = Optional[Callable[[float], None]]


@dataclass(frozen=True)
class Master:
    local_path: Path
    drive_path: Path
    fps: int
    width: int
    height: int
    duration_s: float
    size_bytes: int
    has_audio: bool
    encode_seconds: float
    warnings: list[str]


def command(source: Path, out: Path, info: MediaInfo, config: dict) -> list[str]:
    """ffmpeg command for the master; autorotate (on by default) turns the pixels upright."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
           "-map", "0:v:0",
           # fill/trim by timestamp; start_time=0 pads with the first frame if video starts late
           "-vf", f"fps=fps={info.target_fps}:start_time=0",
           "-c:v", "libx264", "-crf", str(get(config, "video.master_crf")),
           "-preset", get(config, "video.master_preset"), "-pix_fmt", "yuv420p"]
    if info.audio_track_index is None:
        cmd += ["-an"]
    else:
        cmd += ["-map", f"0:a:{info.audio_track_index}",
                # async=1: fill gaps / trim overlaps only, never stretch; first_pts=0 pads a late start
                "-af", f"aresample={get(config, 'audio.master_sample_rate')}:async=1:first_pts=0",
                "-c:a", "aac", "-b:a", AUDIO_BITRATE]
    cmd += ["-max_muxing_queue_size", "4096", "-movflags", "+faststart", "-f", "mp4", str(out)]
    return cmd


def check_cfr(path: Path, fps: int) -> list[str]:
    """Problems with the master's frame timing (empty list = every frame lasts exactly 1/fps)."""
    info = ffmpeg.probe(path)
    video = next(s for s in info["streams"] if s.get("codec_type") == "video")
    tb = Fraction(video["time_base"])
    durations = frame_durations(ffmpeg.packet_pts(path, video["index"]), tb)
    if not durations:
        return ["the master has no frames"]
    want = Fraction(1, fps)
    off = [d for d in durations if abs(d - want) > tb]      # allow one time-base tick
    if off:
        return [f"{len(off)} of {len(durations)} master frames are not 1/{fps} s long "
                f"(e.g. {float(off[0]) * 1000:.2f} ms)"]
    if any("rotation" in side for side in video.get("side_data_list", [])):
        return ["the master still carries rotation metadata"]
    return []


def _describe(local_path: Path, drive_path: Path, fps: int, encode_seconds: float) -> Master:
    out = ffmpeg.probe(local_path)
    video = next(s for s in out["streams"] if s.get("codec_type") == "video")
    has_audio = any(s.get("codec_type") == "audio" for s in out["streams"])
    warnings = [] if has_audio else ["The master has no sound (the recording has no audio track)."]
    return Master(local_path, drive_path, fps, int(video["width"]), int(video["height"]),
                  float(out["format"]["duration"]), local_path.stat().st_size, has_audio,
                  encode_seconds, warnings)


def from_drive(pre: Preflight, stage_in: StageIn, info: MediaInfo,
               progress: Progress = None) -> Master:
    """The master made in an earlier run, copied back to local disk if it isn't there already."""
    local_path = stage_in.local_dir / FILE_NAME
    drive_path = pre.paths.work / stage_in.match_id / FILE_NAME
    size = drive_path.stat().st_size
    if not (local_path.is_file() and local_path.stat().st_size == size):
        try:
            files.copy_file(drive_path, local_path, expected_size=size, progress=progress)
        except OSError as exc:
            raise StageError(TITLE, [f"Could not copy the earlier {FILE_NAME} back from Drive: "
                                     f"{exc.strerror or exc}. Re-run with force re-run ticked."]) from None
    return _describe(local_path, drive_path, info.target_fps, 0.0)


def run(pre: Preflight, stage_in: StageIn, probe: Probe, progress: Progress = None,
        copy_progress: Progress = None) -> Master:
    info = probe.media_info
    local_path = stage_in.local_dir / FILE_NAME
    drive_path = pre.paths.work / stage_in.match_id / FILE_NAME
    partial = files.partial_path(local_path)
    log_path = stage_in.local_dir / "logs" / "s3_master.ffmpeg.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    try:
        ffmpeg.run_with_progress(command(stage_in.local_source, partial, info, pre.config),
                                 info.duration_s, progress, log_path)
    except ffmpeg.ToolError as exc:
        partial.unlink(missing_ok=True)
        raise StageError(TITLE, [f"FFmpeg could not make the master ({exc}). Re-run; if it "
                                 f"fails again, send this message."]) from None
    encode_seconds = time.monotonic() - started

    problems = check_cfr(partial, info.target_fps)
    if problems:
        partial.unlink(missing_ok=True)
        raise StageError(TITLE, [f"The master failed its frame check: {p} (a pipeline bug; "
                                 f"send this message)." for p in problems])
    partial.replace(local_path)

    try:
        files.copy_file(local_path, drive_path, progress=copy_progress)
    except OSError as exc:
        raise StageError(TITLE, [f"Could not save {FILE_NAME} to Drive ({drive_path}): "
                                 f"{exc.strerror or exc}. Check Drive has space, then re-run."]) from None

    return _describe(local_path, drive_path, info.target_fps, encode_seconds)
