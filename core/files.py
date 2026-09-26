"""Safe file writes: every file appears under its final name only once it is complete."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

COPY_CHUNK = 16 * 1024 * 1024


class SizeMismatch(OSError):
    """The copy's size differs from the expected size (e.g. the source is still uploading)."""

    def __init__(self, copied: int, expected: int):
        self.copied, self.expected = copied, expected
        super().__init__(f"copied {copied} bytes, expected {expected}")


def partial_path(path: Path) -> Path:
    return path.with_name(path.name + ".partial")


def _copy_chunks(src: Path, dst: Path, total: int,
                 progress: Optional[Callable[[float], None]]) -> None:
    done = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while chunk := fin.read(COPY_CHUNK):
            fout.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(min(done / total, 1.0))


def copy_file(src: Path, dst: Path, expected_size: Optional[int] = None,
              progress: Optional[Callable[[float], None]] = None) -> None:
    """Copy via `<dst>.partial`, check the size, then rename. Leaves no partial file on failure."""
    expected = src.stat().st_size if expected_size is None else expected_size
    partial = partial_path(dst)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy_chunks(src, partial, expected, progress)
        copied = partial.stat().st_size
        if copied != expected:
            raise SizeMismatch(copied, expected)
        os.replace(partial, dst)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def write_text(path: Path, text: str) -> None:
    """Write via `<path>.partial` and rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = partial_path(path)
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)
