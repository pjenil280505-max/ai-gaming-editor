"""S4 Analysis copy (docs/PHASE0.md): small video + mono WAV made from the master.

One decode of the local master writes both files. Each must end within one
analysis frame of the master. Details in DEC-023.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core import ffmpeg, files
from core.config import get
from core.errors import StageError
from core.s0_preflight import Preflight
from core.s1_stage_in import StageIn
from core.s3_master import Master

TITLE = "S4 Analysis copy"
VIDEO_NAME = "analysis.mp4"
AUDIO_NAME = "analysis.wav"
VIDEO_CRF = "23"

Progress = Optional[Callable[[float], None]]


@dataclass(frozen=True)
class Analysis:
    local_video: Path
    local_audio: Optional[Path]
    drive_video: Path
    drive_audio: Optional[Path]
    width: int
    height: int
    warnings: list[str]


def command(master: Path, has_audio: bool, video_out: Path, audio_out: Path, config: dict) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(master),
           "-map", "0:v:0",
           "-vf", f"scale={get(config, 'analysis.width')}:-2,fps={get(config, 'analysis.fps')}",
           "-c:v", "libx264", "-crf", VIDEO_CRF, "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-an", "-movflags", "+faststart", "-f", "mp4", str(video_out)]
    if has_audio:
        cmd += ["-map", "0:a:0", "-vn", "-ac", str(get(config, "analysis.audio_channels")),
                "-ar", str(get(config, "analysis.audio_rate")), "-c:a", "pcm_s16le",
                "-f", "wav", str(audio_out)]
    return cmd


def _duration(path: Path) -> float:
    return float(ffmpeg.probe(path)["format"]["duration"])


def video_from_drive(pre: Preflight, stage_in: StageIn, progress: Progress = None) -> Path:
    """analysis.mp4 from an earlier run on local disk (for S5), copied back unless already there."""
    local_path = stage_in.local_dir / VIDEO_NAME
    drive_path = pre.paths.work / stage_in.match_id / VIDEO_NAME
    try:
        size = drive_path.stat().st_size
        if not (local_path.is_file() and local_path.stat().st_size == size):
            files.copy_file(drive_path, local_path, expected_size=size, progress=progress)
    except OSError as exc:
        raise StageError(TITLE, [f"Could not copy the earlier {VIDEO_NAME} back from Drive: "
                                 f"{exc.strerror or exc}. Re-run with force re-run ticked."]) from None
    return local_path


def run(pre: Preflight, stage_in: StageIn, master: Master, progress: Progress = None) -> Analysis:
    config = pre.config
    frame_s = 1 / get(config, "analysis.fps")
    local_video = stage_in.local_dir / VIDEO_NAME
    local_audio = stage_in.local_dir / AUDIO_NAME if master.has_audio else None
    partial_video = files.partial_path(local_video)
    partial_audio = files.partial_path(stage_in.local_dir / AUDIO_NAME)
    log_path = stage_in.local_dir / "logs" / "s4_analysis.ffmpeg.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def discard():
        partial_video.unlink(missing_ok=True)
        partial_audio.unlink(missing_ok=True)

    try:
        ffmpeg.run_with_progress(command(master.local_path, master.has_audio, partial_video,
                                         partial_audio, config),
                                 master.duration_s, progress, log_path)
    except ffmpeg.ToolError as exc:
        discard()
        raise StageError(TITLE, [f"FFmpeg could not make the analysis copy ({exc}). Re-run; if "
                                 f"it fails again, send this message."]) from None

    problems = []
    outputs = [(partial_video, VIDEO_NAME)] + ([(partial_audio, AUDIO_NAME)] if master.has_audio else [])
    for path, name in outputs:
        gap = abs(_duration(path) - master.duration_s)
        if gap > frame_s:
            problems.append(f"{name} is {gap:.3f} s longer or shorter than the master "
                            f"(limit {frame_s:.3f} s; a pipeline bug; send this message).")
    if problems:
        discard()
        raise StageError(TITLE, problems)

    partial_video.replace(local_video)
    if local_audio:
        partial_audio.replace(local_audio)

    drive_dir = pre.paths.work / stage_in.match_id
    drive_video = drive_dir / VIDEO_NAME
    drive_audio = drive_dir / AUDIO_NAME if local_audio else None
    try:
        files.copy_file(local_video, drive_video)
        if local_audio:
            files.copy_file(local_audio, drive_audio)
    except OSError as exc:
        raise StageError(TITLE, [f"Could not save the analysis copy to Drive ({drive_dir}): "
                                 f"{exc.strerror or exc}. Check Drive has space, then re-run."]) from None

    video = next(s for s in ffmpeg.probe(local_video)["streams"] if s.get("codec_type") == "video")
    warnings = [] if local_audio else ["No analysis.wav: the master has no sound."]
    return Analysis(local_video, local_audio, drive_video, drive_audio,
                    int(video["width"]), int(video["height"]), warnings)
