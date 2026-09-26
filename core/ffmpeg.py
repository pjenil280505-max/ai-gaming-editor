"""Thin wrappers around the ffmpeg / ffprobe command-line tools."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional


class ToolError(RuntimeError):
    """ffmpeg/ffprobe is missing or exited with an error; message is its last stderr line."""


def available(tool: str) -> bool:
    return shutil.which(tool) is not None


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    if not available(cmd[0]):
        raise ToolError(f"{cmd[0]} is not installed")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        lines = [line for line in result.stderr.strip().splitlines() if line.strip()]
        raise ToolError(lines[-1] if lines else f"{cmd[0]} exited with code {result.returncode}")
    return result


def version(tool: str) -> str:
    """First line of `<tool> -version`, e.g. 'ffmpeg version 6.1.1-3ubuntu5 Copyright ...'."""
    first = run([tool, "-hide_banner", "-version"]).stdout.splitlines()[0]
    return first.split(" Copyright")[0]


def probe(path: Path) -> dict:
    """ffprobe format + streams as a dict."""
    result = run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                  "-of", "json", str(path)])
    return json.loads(result.stdout)


def packet_pts(path: Path, stream_index: int) -> list[int]:
    """Presentation timestamps (stream time-base units) of one stream's packets, in file order."""
    result = run(["ffprobe", "-v", "error", "-select_streams", str(stream_index),
                  "-show_entries", "packet=pts", "-of", "csv=p=0", str(path)])
    pts = []
    for line in result.stdout.splitlines():
        field = line.split(",")[0].strip()      # packets with side data add extra fields
        if field.lstrip("-").isdigit():         # skips N/A
            pts.append(int(field))
    return pts


def run_with_progress(cmd: list[str], duration_s: float,
                      progress: Optional[Callable[[float], None]] = None,
                      log_path: Optional[Path] = None) -> None:
    """Run an ffmpeg command, reporting the fraction of `duration_s` done so far.

    Adds `-progress pipe:1 -nostats`; stderr goes to `log_path` (or is discarded
    unread), so a chatty ffmpeg can never block on a full pipe.
    """
    if not available(cmd[0]):
        raise ToolError(f"{cmd[0]} is not installed")
    full = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    log = open(log_path, "w", encoding="utf-8") if log_path else subprocess.DEVNULL
    try:
        with subprocess.Popen(full, stdout=subprocess.PIPE, stderr=log, text=True) as proc:
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.isdigit() and progress and duration_s > 0:
                    progress(min(int(value) / (duration_s * 1e6), 1.0))
            code = proc.wait()
    finally:
        if log_path:
            log.close()
    if code != 0:
        lines = []
        if log_path and log_path.exists():
            lines = [x for x in log_path.read_text(errors="replace").splitlines() if x.strip()]
        raise ToolError(lines[-1] if lines else f"{cmd[0]} exited with code {code}")
