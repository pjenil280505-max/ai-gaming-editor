"""Thin wrappers around the ffmpeg / ffprobe command-line tools."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


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
