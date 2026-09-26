"""S1 Stage-in (docs/PHASE0.md): compute match_id, copy the recording to local disk, verify size.

Runs on every run, writes no marker and copies nothing to Drive (DEC-012).
The inbox file is only read, never modified (Phase 0 rule).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core.errors import StageError
from core.s0_preflight import Preflight, human_size

TITLE = "S1 Stage-in"
HASH_BYTES = 8 * 1024 * 1024        # "8 MB" in the match-ID rule, read as 8 MiB (DEC-014)
COPY_CHUNK = 16 * 1024 * 1024


@dataclass(frozen=True)
class StageIn:
    match_id: str
    local_dir: Path
    local_source: Path
    size_bytes: int
    copied: bool            # False when an identical-size local copy was reused


def match_id(path: Path) -> str:
    """<YYYYMMDD-HHMM of mtime, UTC>-<6 hex of SHA-256(size, first 8 MiB, last 8 MiB)>."""
    stat = path.stat()
    stamp = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y%m%d-%H%M")
    digest = hashlib.sha256(str(stat.st_size).encode("ascii"))
    with open(path, "rb") as f:
        digest.update(f.read(HASH_BYTES))
        f.seek(max(0, stat.st_size - HASH_BYTES))
        digest.update(f.read(HASH_BYTES))
    return f"{stamp}-{digest.hexdigest()[:6]}"


def _copy(src: Path, dst: Path, total: int, progress: Optional[Callable[[float], None]]) -> None:
    done = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while chunk := fin.read(COPY_CHUNK):
            fout.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(min(done / total, 1.0))


def run(pre: Preflight, force: bool = False,
        progress: Optional[Callable[[float], None]] = None) -> StageIn:
    try:
        mid = match_id(pre.source)
    except OSError as exc:
        raise StageError(TITLE, [f"Could not read {pre.source.name} from Drive: {exc.strerror or exc}. "
                                 f"Check the upload finished, then re-run."]) from None

    local_dir = pre.paths.local_work / mid
    local_source = local_dir / ("source" + pre.source.suffix.lower())
    size = pre.source.stat().st_size

    if not force and local_source.is_file() and local_source.stat().st_size == size:
        return StageIn(mid, local_dir, local_source, size, copied=False)

    partial = local_dir / (local_source.name + ".partial")
    try:
        local_dir.mkdir(parents=True, exist_ok=True)
        _copy(pre.source, partial, size, progress)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise StageError(TITLE, [f"Copying {pre.source.name} to local disk failed: "
                                 f"{exc.strerror or exc}. Re-run; if it keeps failing, "
                                 f"re-upload the recording."]) from None

    copied_size = partial.stat().st_size
    if copied_size != size:
        partial.unlink(missing_ok=True)
        raise StageError(TITLE, [
            f"The local copy is {human_size(copied_size)} but the recording on Drive is "
            f"{human_size(size)} ({copied_size} vs {size} bytes). The file may still be "
            f"uploading; wait for the upload to finish and re-run."])
    os.replace(partial, local_source)
    return StageIn(mid, local_dir, local_source, size, copied=True)
