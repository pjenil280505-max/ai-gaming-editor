"""S6 Stage-out (docs/PHASE0.md, DEC-011): run_report.json, summary.txt and logs on Drive.

Runs at the end of every run, including runs that stop with an error, and
writes no marker: the report describes the run that just happened.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core import files
from core.config import config_fingerprint
from core.contracts import (STAGES, Environment, MediaInfo, RunReport, StageResult, Totals)
from core.errors import StageError
from core.resume import STAGE_TITLES
from core.s0_preflight import Preflight

TITLE = STAGE_TITLES["s6_stage_out"]
REPORT_NAME = "run_report.json"
SUMMARY_NAME = "summary.txt"
LOG_DIR = "logs"
WATCH_HINT = ("Watch master.mp4 (full quality). analysis.mp4 and analysis.wav are small "
              "working copies for the pipeline, not for watching.")


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class StageRecord:
    name: str
    status: str                       # pass / warn / fail
    started_at: datetime
    finished_at: datetime
    skipped: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    note: str = ""                    # one line for summary.txt


class RunLog:
    """Prints lines (like print) and remembers them and each stage's outcome for the report."""

    def __init__(self, log: Callable[[str], None]):
        self._log = log
        self.lines: list[str] = []
        self.records: list[StageRecord] = []
        self.started = now()

    def __call__(self, line: str) -> None:
        self.lines.append(line)
        self._log(line)

    def done(self, name: str, started: datetime, warnings: list[str] = (), note: str = "") -> None:
        self.records.append(StageRecord(name, "warn" if warnings else "pass", started, now(),
                                        warnings=list(warnings), note=note))

    def skipped(self, name: str, status: str, note: str, warnings: list[str] = ()) -> None:
        moment = now()
        if status == "warn" and not warnings:
            note += " (it had warnings then; see that run's log in logs/)"
        self.records.append(StageRecord(name, status, moment, moment, skipped=True,
                                        warnings=list(warnings), note=note))

    def failed(self, name: str, started: datetime, errors: list[str]) -> None:
        self.records.append(StageRecord(name, "fail", started, now(), errors=list(errors)))


def clock(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def build_report(run: RunLog, run_id: str, match_id: str, version: Optional[str],
                 pre: Preflight, media_info: Optional[MediaInfo], finished: datetime) -> RunReport:
    footage = media_info.duration_s / 60 if media_info else 0.0
    processing = (finished - run.started).total_seconds() / 60
    order = {name: i for i, name in enumerate(STAGES)}
    records = sorted(run.records, key=lambda r: order[r.name])
    return RunReport(
        run_id=run_id, match_id=match_id, code_version=version or "unknown",
        config_fingerprint=config_fingerprint(pre.config),
        environment=Environment(platform=pre.platform, cpu_count=pre.cpu_count, gpu=pre.gpu,
                                ffmpeg_version=pre.ffmpeg_version),
        stages=[StageResult(name=r.name, status=r.status,
                            started_at=r.started_at.isoformat(timespec="seconds"),
                            finished_at=r.finished_at.isoformat(timespec="seconds"),
                            duration_s=round((r.finished_at - r.started_at).total_seconds(), 3),
                            skipped=r.skipped, warnings=r.warnings, errors=r.errors)
                for r in records],
        qc=[],                                   # S5 QC arrives in 0.5
        totals=Totals(footage_minutes=round(footage, 3), processing_minutes=round(processing, 3),
                      minutes_per_footage_minute=round(processing / footage, 3) if footage else None),
    )


def summary_text(report: RunReport, run: RunLog, pre: Preflight,
                 media_info: Optional[MediaInfo]) -> str:
    failed = [s for s in report.stages if s.status == "fail"]
    warned = any(s.status == "warn" for s in report.stages)
    result = (f"FAILED at {STAGE_TITLES[failed[0].name]}" if failed
              else "WARN" if warned else "PASS")
    notes = {r.name: r.note for r in run.records}
    lines = [
        f"Match {report.match_id} · run {report.run_id} · code {report.code_version[:7]}",
        f"Recording: {pre.source.name}"
        + (f" ({clock(media_info.duration_s)})" if media_info else ""),
        f"Result: {result}",
        "",
        "Stages:",
    ]
    for s in report.stages:
        state = "skipped" if s.skipped else s.status
        lines.append(f"  {STAGE_TITLES[s.name]:<17} {state:<8} {clock(s.duration_s):>6}"
                     + (f"  {notes[s.name]}" if notes.get(s.name) else ""))
    problems = [(s.name, w) for s in report.stages for w in s.warnings + s.errors]
    if problems:
        lines += ["", "Warnings and errors:"]
        lines += [f"  {STAGE_TITLES[name]}: {text}" for name, text in problems]
    t = report.totals
    lines += ["", f"Processing: {t.processing_minutes:.1f} min for {t.footage_minutes:.1f} min of footage"
              + (f" ({t.minutes_per_footage_minute:.2f} min per footage minute)"
                 if t.minutes_per_footage_minute is not None else ""),
              "", WATCH_HINT, ""]
    return "\n".join(lines)


def write(run: RunLog, run_id: str, match_id: str, version: Optional[str], pre: Preflight,
          media_info: Optional[MediaInfo], local_dir: Path, drive_dir: Path) -> tuple[Path, Path]:
    """Copy logs, then write run_report.json and summary.txt to the Drive work dir."""
    started = now()
    try:
        logs = drive_dir / LOG_DIR
        logs.mkdir(parents=True, exist_ok=True)
        for log_file in sorted((local_dir / LOG_DIR).glob("*.log")):
            shutil.copyfile(log_file, logs / log_file.name)
        files.write_text(logs / f"run-{run_id}.txt", "\n".join(run.lines) + "\n")
        run.done("s6_stage_out", started)
        report = build_report(run, run_id, match_id, version, pre, media_info, now())
        problems = report.validate()
        if problems:
            raise ValueError(f"run report failed its contract: {problems}")
        report_path, summary_path = drive_dir / REPORT_NAME, drive_dir / SUMMARY_NAME
        files.write_text(report_path, report.to_json())
        files.write_text(summary_path, summary_text(report, run, pre, media_info))
    except OSError as exc:
        raise StageError(TITLE, [f"Could not save the run report to Drive ({drive_dir}): "
                                 f"{exc.strerror or exc}. The video files are fine; re-run to "
                                 f"write the report."]) from None
    return report_path, summary_path
