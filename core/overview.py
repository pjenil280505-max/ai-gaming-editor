"""C6 (docs/PHASE0.md): every match on Drive and every S3 encode so far, in one screen.

The match table comes from each match's run_report.json (its latest run); the
encode table comes from the run logs in logs/ (every run), because the report
is overwritten each run and a skipped S3 records no speed. Together they give
the Phase 0 report its A1–A6 and D1 evidence from one screenshot. DEC-032.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.config import ConfigError, load_config, resolve_paths
from core.contracts import MATCH_ID, ContractError, RunReport
from core.report import LOG_DIR, REPORT_NAME, result
from core.run import D1_LIMIT

_CPU = re.compile(r"; (\d+) CPUs(?: \((.*?)\))?; GPU")
_S3 = re.compile(r"S3 Master: encoding (\d+):(\d\d) at \d+ fps")
_SPEED = re.compile(r"encoding took \d+:\d\d = ([\d.]+)x real time")
_RUN_LOG = re.compile(r"^run-(\d{8}T\d{6}Z)\.txt$")


@dataclass(frozen=True)
class Encode:
    run_id: str                 # YYYYMMDDTHHMMSSZ
    match_id: str
    footage_s: int
    speed: float                # S3 encode time / footage time
    cpus: Optional[int]         # None in logs from before the CPU was printed
    cpu_model: Optional[str]    # None before 0.5 (DEC-029)


def clock(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def encodes(match_dir: Path) -> list[Encode]:
    """Every S3 encode recorded in a match's run logs, oldest first."""
    found = []
    logs = match_dir / LOG_DIR
    for path in sorted(logs.iterdir()) if logs.is_dir() else []:
        name = _RUN_LOG.match(path.name)
        if not name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        s3, speed = _S3.search(text), _SPEED.search(text)
        if not (s3 and speed):
            continue                                    # S3 skipped or failed in that run
        cpu = _CPU.search(text)
        model = cpu.group(2) if cpu else None
        found.append(Encode(name.group(1), match_dir.name, int(s3.group(1)) * 60 + int(s3.group(2)),
                            float(speed.group(1)), int(cpu.group(1)) if cpu else None,
                            None if model in (None, "model unknown") else model))
    return found


def _qc(report: RunReport) -> str:
    if not report.qc:
        return "not run"
    counts = {s: sum(1 for c in report.qc if c.status == s) for s in ("pass", "warn", "fail")}
    return ", ".join(f"{n} {s}" for s, n in counts.items() if n)


def _match_row(match_dir: Path) -> str:
    try:
        report = RunReport.from_json((match_dir / REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ContractError):
        return f"  {match_dir.name}  no readable {REPORT_NAME} (the run stopped before the report)"
    footage = clock(report.totals.footage_minutes * 60)
    return f"  {match_dir.name}  {footage:>6}  {result(report):<9}  {_qc(report)}"


def text(config_path: Optional[Path] = None) -> str:
    """C6's output: matches, S3 encodes and where the files are, or why it can't be shown."""
    try:
        work = resolve_paths(load_config(config_path)).work
    except ConfigError as exc:
        return str(exc)
    if not work.is_dir():
        return f"Work folder {work} not found. Run cell C1, then process a recording with C5."
    matches = sorted(p for p in work.iterdir() if p.is_dir() and re.match(MATCH_ID, p.name))
    if not matches:
        return f"No matches in {work} yet. Process a recording with C4 and C5 first."

    lines = [f"Matches in {work} (latest run of each; files in <folder>/<match>/):",
             f"  {'match':<20}  {'length':>6}  {'result':<9}  QC checks"]
    lines += [_match_row(m) for m in matches]

    runs = sorted((e for m in matches for e in encodes(m)), key=lambda e: e.run_id)
    lines += ["", "S3 encodes, every run (from logs/):"]
    if not runs:
        lines.append("  none recorded yet")
    else:
        lines.append(f"  {'run (UTC)':<16}  {'match':<20}  {'length':>6}  {'speed':>6}  machine")
        for e in runs:
            when = datetime.strptime(e.run_id, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d %H:%M")
            machine = (f"{e.cpus} CPUs, " if e.cpus else "") + (e.cpu_model or "CPU not recorded")
            lines.append(f"  {when:<16}  {e.match_id:<20}  {clock(e.footage_s):>6}  "
                         f"{e.speed:>5.2f}x  {machine}")
        speeds = [e.speed for e in runs]
        over = sum(1 for s in speeds if s > D1_LIMIT)
        lines.append(f"  {len(runs)} encodes: {min(speeds):.2f}x to {max(speeds):.2f}x real time; "
                     f"{over} over the D1 limit of {D1_LIMIT}x")
    return "\n".join(lines)
