"""S0 Preflight (docs/PHASE0.md): check the machine and inputs before touching footage.

Runs on every run and writes no marker (DEC-010). Collects every problem
before failing, so the owner can fix them all at once.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from core import ffmpeg
from core.config import ConfigError, Paths, load_config, resolve_paths, with_overrides
from core.errors import StageError

TITLE = "S0 Preflight"
DISK_FACTOR = 3          # free local disk must be >= 3 x the recording's size


@dataclass(frozen=True)
class Preflight:
    config: dict
    paths: Paths
    source: Path
    source_size_bytes: int
    free_disk_bytes: int
    ffmpeg_version: str
    ffprobe_version: str
    cpu_count: int
    gpu: Optional[str]
    gpu_encoder: bool       # recorded only (DEC-017)
    platform: str


def human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    raise AssertionError("unreachable")


def free_bytes(path: Path) -> int:
    """Free space on the disk that holds `path` (or its nearest existing parent)."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free


def gpu_name() -> Optional[str]:
    if not ffmpeg.available("nvidia-smi"):
        return None
    try:
        out = ffmpeg.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=30)
    except (ffmpeg.ToolError, OSError, subprocess.TimeoutExpired):
        return None
    names = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    return names[0] if names else None


def gpu_encoder_works() -> bool:
    """True if FFmpeg can actually encode H.264 on the GPU (a tiny test encode)."""
    try:
        encoders = ffmpeg.run(["ffmpeg", "-hide_banner", "-encoders"]).stdout
        if "h264_nvenc" not in encoders:
            return False
        ffmpeg.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                    "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=60)
        return True
    except (ffmpeg.ToolError, OSError, subprocess.TimeoutExpired):
        return False


def run(source_name: str, overrides: Optional[dict[str, Any]] = None,
        config_path: Optional[Path] = None) -> Preflight:
    """Check everything S1–S2 need; raise StageError listing every problem."""
    problems: list[str] = []

    config = paths = None
    try:
        config = load_config(config_path) if config_path else load_config()
        if overrides:
            config = with_overrides(config, overrides)
        paths = resolve_paths(config)
    except ConfigError as exc:
        problems += [f"Config: {p}" for p in exc.problems]

    versions = {}
    for tool in ("ffmpeg", "ffprobe"):
        try:
            versions[tool] = ffmpeg.version(tool)
        except (ffmpeg.ToolError, OSError, IndexError):
            problems.append(f"{tool} is not installed or does not run. Colab ships it; "
                            f"on the cloud box see docs/cloud-setup.md.")

    source, size, free = None, 0, 0
    if paths is not None:
        if not paths.drive_root.is_dir():
            problems.append(f"Google Drive folder {paths.drive_root} not found. Run cell C1 to "
                            f"mount Drive and check the AIEditor folder exists in My Drive.")
        elif not paths.inbox.is_dir():
            problems.append(f"Inbox folder {paths.inbox} not found. Create it in Drive and "
                            f"upload recordings there.")
        elif not source_name or Path(source_name).name != source_name:
            problems.append(f"Recording name '{source_name}' is not a plain file name in the inbox.")
        elif not (paths.inbox / source_name).is_file():
            problems.append(f"Recording '{source_name}' not found in {paths.inbox}.")
        else:
            source = paths.inbox / source_name
            size = source.stat().st_size
            free = free_bytes(paths.local_work)
            if free < DISK_FACTOR * size:
                problems.append(
                    f"Not enough free space on Colab's local disk: {human_size(free)} free, "
                    f"{human_size(DISK_FACTOR * size)} needed ({DISK_FACTOR} x the "
                    f"{human_size(size)} recording). Use a shorter recording or a runtime "
                    f"with more disk.")

    if problems:
        raise StageError(TITLE, problems)

    gpu = gpu_name()
    return Preflight(
        config=config, paths=paths, source=source, source_size_bytes=size,
        free_disk_bytes=free, ffmpeg_version=versions["ffmpeg"],
        ffprobe_version=versions["ffprobe"], cpu_count=os.cpu_count() or 1,
        gpu=gpu, gpu_encoder=gpu_encoder_works() if gpu else False,
        platform=platform.platform(),
    )
