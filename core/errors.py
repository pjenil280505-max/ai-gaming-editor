"""Errors shown to the owner. Messages are plain English and say what to do next."""

from __future__ import annotations


class StageError(RuntimeError):
    """A stage cannot continue; `problems` lists every reason found."""

    def __init__(self, stage_title: str, problems: list[str]):
        self.stage_title = stage_title
        self.problems = list(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"{stage_title} failed:\n{lines}")
