"""S2 Probe (docs/PHASE0.md): measure the local copy and write media_info.json.

Every failure check runs before anything is written, so a bad recording
leaves no media_info.json behind. Detection rules are in DEC-016.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from fractions import Fraction
from math import gcd
from pathlib import Path
from typing import Optional

from core import ffmpeg, files
from core.config import get
from core.contracts import AudioTrack, FrameDurationStats, MediaInfo, VideoInfo
from core.errors import StageError
from core.s0_preflight import Preflight
from core.s1_stage_in import StageIn

TITLE = "S2 Probe"
FILE_NAME = "media_info.json"
MIN_DURATION_S = 5.0
VFR_DEVIATION = Fraction(1, 10)   # a frame "differs" if its duration is >10 % off the median
VFR_FRACTION = 0.01               # variable frame rate if more than 1 % of frames differ
AUTO_60_FROM_FPS = 45             # auto target: median fps >= 45 -> 60, else 30
FPS_WARN_DELTA = 1.0              # warn when the master fps differs from the source by more


@dataclass(frozen=True)
class Probe:
    media_info: MediaInfo
    local_path: Path
    drive_path: Path


def _ratio(text: Optional[str], sep: str) -> Optional[Fraction]:
    try:
        num, den = (int(x) for x in (text or "").split(sep))
        return Fraction(num, den) if num > 0 and den > 0 else None
    except ValueError:
        return None


def rotation_cw(stream: dict) -> int:
    """Clockwise display rotation (0/90/180/270) from the display matrix side data."""
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            return int(round(-float(side["rotation"]) / 90)) * 90 % 360
    return 0


def display_aspect(width: int, height: int, sar_text: Optional[str], rotation: int) -> str:
    sar = _ratio(sar_text, ":") or Fraction(1)
    shown_w, shown_h = width * sar.numerator, height * sar.denominator
    if rotation in (90, 270):
        shown_w, shown_h = shown_h, shown_w
    g = gcd(shown_w, shown_h)
    return f"{shown_w // g}:{shown_h // g}"


def frame_durations(pts: list[int], time_base: Fraction) -> list[Fraction]:
    ordered = sorted(pts)
    return [(b - a) * time_base for a, b in zip(ordered, ordered[1:])]


def vfr_verdict(durations: list[Fraction]) -> tuple[bool, str]:
    median = statistics.median(durations)
    off = sum(1 for d in durations if abs(d - median) > VFR_DEVIATION * median)
    share = off / len(durations)
    is_vfr = share > VFR_FRACTION
    ms = [float(d) * 1000 for d in (min(durations), median, max(durations))]
    evidence = (f"{off} of {len(durations)} frame durations ({share:.1%}) differ from the median "
                f"{ms[1]:.2f} ms by more than 10% (range {ms[0]:.2f}-{ms[2]:.2f} ms); "
                f"more than 1% means variable frame rate.")
    return is_vfr, evidence


def choose_target_fps(setting, median_fps: float) -> int:
    if setting == "auto":
        return 60 if median_fps >= AUTO_60_FROM_FPS else 30
    return int(setting)


def _end(stream: dict) -> Optional[float]:
    """Where a stream ends on the file's timeline (start + duration); None if unrecorded."""
    try:
        return float(stream.get("start_time") or 0.0) + float(stream["duration"])
    except (KeyError, TypeError, ValueError):
        return None


def _video_stream(streams: list[dict]) -> Optional[dict]:
    for s in streams:
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic"):
            return s
    return None


def measure(path: Path, config: dict, name: str) -> MediaInfo:
    """Build media_info for `path`; raise StageError on the PHASE0 failure conditions."""
    try:
        info = ffmpeg.probe(path)
    except ffmpeg.ToolError as exc:
        detail = str(exc).replace(f"{path}: ", "")
        raise StageError(TITLE, [f"Could not read {name} ({detail}). The file may be incomplete "
                                 f"or not a video; re-upload it."]) from None

    streams = info.get("streams", [])
    video = _video_stream(streams)
    if video is None:
        raise StageError(TITLE, [f"{name} has no video stream."])
    try:
        duration = float(info.get("format", {}).get("duration") or video.get("duration"))
    except (TypeError, ValueError):
        raise StageError(TITLE, [f"Could not tell how long {name} is; re-upload it."]) from None
    if duration < MIN_DURATION_S:
        raise StageError(TITLE, [f"{name} is {duration:.1f} s long; recordings must be at "
                                 f"least {MIN_DURATION_S:.0f} s."])

    time_base = _ratio(video.get("time_base"), "/")
    pts = ffmpeg.packet_pts(path, video["index"])
    if time_base is None or len(pts) < 2:
        raise StageError(TITLE, [f"{name} has no readable video frames; re-upload it."])
    durations = frame_durations(pts, time_base)
    if min(durations) <= 0:
        durations = [d for d in durations if d > 0]      # duplicate timestamps
    is_vfr, evidence = vfr_verdict(durations)
    median = statistics.median(durations)
    median_fps = float(1 / median)

    warnings: list[str] = []
    target = choose_target_fps(get(config, "video.target_fps"), median_fps)
    if abs(median_fps - target) > FPS_WARN_DELTA:
        action = "duplicated" if target > median_fps else "dropped"
        warnings.append(f"The recording runs at about {median_fps:.1f} fps; the master will be "
                        f"{target} fps, so some frames will be {action}.")

    video_start, video_end = float(video.get("start_time") or 0.0), _end(video)
    audio = [x for x in streams if x.get("codec_type") == "audio"]
    ends = [_end(s) for s in audio]
    tracks = [
        AudioTrack(index=i, codec=s.get("codec_name", "unknown"),
                   sample_rate=int(s.get("sample_rate") or 0), channels=int(s.get("channels") or 0),
                   start_offset_s=round(float(s.get("start_time") or 0.0) - video_start, 6),
                   end_offset_s=(None if end is None or video_end is None
                                 else round(end - video_end, 6)))
        for i, (s, end) in enumerate(zip(audio, ends))
    ]
    wanted = get(config, "audio.track_index")
    chosen: Optional[int] = None
    if not tracks:
        warnings.append("No audio track found: the master will have no sound (video-only mode).")
    elif wanted >= len(tracks):
        raise StageError(TITLE, [
            f"audio.track_index is {wanted} but {name} has only {len(tracks)} audio track(s) "
            f"(numbered 0 to {len(tracks) - 1}). Change audio.track_index in configs/pipeline.yaml."])
    else:
        chosen = wanted
        if len(tracks) > 1:
            listing = "; ".join(f"{t.index} = {t.codec} {t.sample_rate} Hz {t.channels} ch"
                                for t in tracks)
            warnings.append(f"Found {len(tracks)} audio tracks ({listing}); using track {chosen} "
                            f"(audio.track_index).")
        offset = tracks[chosen].start_offset_s
        if abs(offset) > 1 / target:
            side = "after" if offset > 0 else "before"
            warnings.append(f"Audio starts {abs(offset):.3f} s {side} the video; S3 corrects this "
                            f"when making the master.")

    rotation = rotation_cw(video)
    media_info = MediaInfo(
        container=info.get("format", {}).get("format_name", "unknown"),
        duration_s=duration,
        size_bytes=path.stat().st_size,
        video=VideoInfo(
            codec=video.get("codec_name", "unknown"),
            width=int(video.get("width") or 0),
            height=int(video.get("height") or 0),
            rotation=rotation,
            display_aspect=display_aspect(int(video.get("width") or 1), int(video.get("height") or 1),
                                          video.get("sample_aspect_ratio"), rotation),
            r_frame_rate=video.get("r_frame_rate", "0/0"),
            avg_frame_rate=video.get("avg_frame_rate", "0/0"),
            frame_durations_s=FrameDurationStats(
                min=float(min(durations)), median=float(median), max=float(max(durations)),
                stdev=statistics.pstdev(float(d) for d in durations)),
            is_vfr=is_vfr,
            vfr_evidence=evidence,
        ),
        audio_tracks=tracks,
        target_fps=target,
        audio_track_index=chosen,
        warnings=warnings,
    )
    problems = media_info.validate()
    if problems:
        raise StageError(TITLE, [f"{name} produced an unexpected probe result (a pipeline bug; "
                                 f"send this message): " + "; ".join(problems)])
    return media_info


def run(pre: Preflight, stage_in: StageIn) -> Probe:
    media_info = measure(stage_in.local_source, pre.config, pre.source.name)
    text = media_info.to_json()
    local_path = stage_in.local_dir / FILE_NAME
    drive_path = pre.paths.work / stage_in.match_id / FILE_NAME
    files.write_text(local_path, text)
    try:
        files.write_text(drive_path, text)
    except OSError as exc:
        raise StageError(TITLE, [f"Could not save {FILE_NAME} to Drive ({drive_path}): "
                                 f"{exc.strerror or exc}. Check Drive is mounted and not full."]) from None
    return Probe(media_info, local_path, drive_path)
