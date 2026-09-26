"""S1 Stage-in (docs/PHASE0.md): compute match_id, copy the recording to local disk, verify size.

Runs on every run, writes no marker and copies nothing to Drive (DEC-012).
The inbox file is only read, never modified (Phase 0 rule).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core import files
from core.errors import StageError
from core.s0_preflight import Preflight, human_size

TITLE = "S1 Stage-in"
HASH_BYTES = 8 * 1024 * 1024        # "8 MB" in the match-ID rule, read as 8 MiB (DEC-014)
# Recording time in the file name, e.g. Record_2026-08-21-21-54-38_<id>.mp4 (DEC-021)
NAME_TIME = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})(?!\d)")
FROM_NAME = "recording time in the file name"
FROM_MTIME = "file modified time on Drive (UTC); the file name has no recording time"


@dataclass(frozen=True)
class StageIn:
    match_id: str
    local_dir: Path
    local_source: Path
    size_bytes: int
    copied: bool            # False when an identical-size local copy was reused
    time_source: str        # where the match-ID time came from (FROM_NAME or FROM_MTIME)


def recording_time(name: str) -> Optional[datetime]:
    """YYYY-MM-DD-HH-MM-SS written in a file name (phone local time), if any valid one."""
    for found in NAME_TIME.finditer(name):
        try:
            return datetime(*(int(part) for part in found.groups()))
        except ValueError:          # e.g. month 13: not a time
            continue
    return None


def match_id(path: Path) -> str:
    return match_id_with_source(path)[0]


def match_id_with_source(path: Path) -> tuple[str, str]:
    """<YYYYMMDD-HHMM>-<6 hex of SHA-256(size, first 8 MiB, last 8 MiB)>, and the time's source.

    The time is the recording time in the file name when there is one (DEC-021),
    otherwise the file's modified time in UTC (DEC-014).
    """
    stat = path.stat()
    named = recording_time(path.name)
    if named is not None:
        stamp, source = named.strftime("%Y%m%d-%H%M"), FROM_NAME
    else:
        stamp = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y%m%d-%H%M")
        source = FROM_MTIME
    digest = hashlib.sha256(str(stat.st_size).encode("ascii"))
    with open(path, "rb") as f:
        digest.update(f.read(HASH_BYTES))
        f.seek(max(0, stat.st_size - HASH_BYTES))
        digest.update(f.read(HASH_BYTES))
    return f"{stamp}-{digest.hexdigest()[:6]}", source


def run(pre: Preflight, force: bool = False,
        progress: Optional[Callable[[float], None]] = None) -> StageIn:
    try:
        mid, source = match_id_with_source(pre.source)
    except OSError as exc:
        raise StageError(TITLE, [f"Could not read {pre.source.name} from Drive: {exc.strerror or exc}. "
                                 f"Check the upload finished, then re-run."]) from None

    local_dir = pre.paths.local_work / mid
    local_source = local_dir / ("source" + pre.source.suffix.lower())
    size = pre.source.stat().st_size

    if not force and local_source.is_file() and local_source.stat().st_size == size:
        return StageIn(mid, local_dir, local_source, size, copied=False, time_source=source)

    try:
        files.copy_file(pre.source, local_source, expected_size=size, progress=progress)
    except files.SizeMismatch as exc:
        raise StageError(TITLE, [
            f"The local copy is {human_size(exc.copied)} but the recording on Drive is "
            f"{human_size(size)} ({exc.copied} vs {size} bytes). The file may still be "
            f"uploading; wait for the upload to finish and re-run."]) from None
    except OSError as exc:
        raise StageError(TITLE, [f"Copying {pre.source.name} to local disk failed: "
                                 f"{exc.strerror or exc}. Re-run; if it keeps failing, "
                                 f"re-upload the recording."]) from None
    return StageIn(mid, local_dir, local_source, size, copied=True, time_source=source)
