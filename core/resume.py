"""Resume rule (docs/PHASE0.md, DEC-010..012, DEC-025): stage markers and the run plan.

A stage from S2 on is skipped only if its marker on Drive exists, is valid, and
its input fingerprint, config fingerprint and code version all match, and
every output it lists is on Drive at its recorded size. The first stage that
fails the test, and every stage after it, runs again. Markers are written only
after the stage's outputs are on Drive.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from core import files
from core.config import stage_fingerprint
from core.contracts import ContractError, OutputFile, StageMarker

MARKER_DIR = "stages"
HASH_BYTES = 8 * 1024 * 1024
REPO = Path(__file__).resolve().parent.parent

# Stages built so far that follow the resume rule, in run order (S5 arrives in 0.5).
RESUMABLE = ("s2_probe", "s3_master", "s4_analysis")

STAGE_TITLES = {
    "s0_preflight": "S0 Preflight", "s1_stage_in": "S1 Stage-in", "s2_probe": "S2 Probe",
    "s3_master": "S3 Master", "s4_analysis": "S4 Analysis copy", "s5_qc": "S5 QC",
    "s6_stage_out": "S6 Stage-out",
}


def code_version() -> Optional[str]:
    """Git commit of the running code, or None when it can't be read (then nothing is skipped)."""
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and len(sha) == 40 else None


def file_fingerprint(path: Path) -> str:
    """SHA-256 hex of the size, first 8 MiB and last 8 MiB (reads at most 16 MiB)."""
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode("ascii"))
    with open(path, "rb") as f:
        digest.update(f.read(HASH_BYTES))
        f.seek(max(0, size - HASH_BYTES))
        digest.update(f.read(HASH_BYTES))
    return digest.hexdigest()


def input_files(stage: str, drive_dir: Path, source: Path) -> dict[str, Path]:
    """What each stage reads; fingerprints are taken from the Drive copies."""
    return {
        "s2_probe": {"source": source},
        "s3_master": {"source": source, "media_info.json": drive_dir / "media_info.json"},
        "s4_analysis": {"master.mp4": drive_dir / "master.mp4"},
    }[stage]


def input_fingerprint(stage: str, drive_dir: Path, source: Path) -> Optional[str]:
    """Combined fingerprint of a stage's inputs; None if one is missing."""
    parts = []
    for name, path in sorted(input_files(stage, drive_dir, source).items()):
        if not path.is_file():
            return None
        parts.append(f"{name}={file_fingerprint(path)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def marker_path(drive_dir: Path, stage: str) -> Path:
    return drive_dir / MARKER_DIR / f"{stage}.done.json"


def write_marker(drive_dir: Path, stage: str, status: str, input_fp: str, config_fp: str,
                 version: Optional[str], outputs: list[Path],
                 started_at: datetime, finished_at: datetime) -> StageMarker:
    """Record a finished stage. Call only after every output is on Drive."""
    marker = StageMarker(
        stage=stage, status=status, input_fingerprint=input_fp, config_fingerprint=config_fp,
        code_version=version or "unknown",
        outputs=[OutputFile(path=p.relative_to(drive_dir).as_posix(), size_bytes=p.stat().st_size)
                 for p in outputs],
        started_at=started_at.isoformat(timespec="seconds"),
        finished_at=finished_at.isoformat(timespec="seconds"),
    )
    problems = marker.validate()
    if problems:
        raise ValueError(f"invalid stage marker for {stage}: {problems}")
    files.write_text(marker_path(drive_dir, stage), marker.to_json())
    return marker


def read_marker(drive_dir: Path, stage: str) -> Optional[StageMarker]:
    try:
        return StageMarker.from_json(marker_path(drive_dir, stage).read_text(encoding="utf-8"))
    except (ContractError, OSError):
        return None


def why_not_done(stage: str, drive_dir: Path, source: Path, config: dict,
                 version: Optional[str]) -> Optional[str]:
    """None if the stage can be skipped; otherwise a plain-English reason it must run."""
    path = marker_path(drive_dir, stage)
    if not path.is_file():
        return "no record of a finished run"
    marker = read_marker(drive_dir, stage)
    if marker is None:
        return "its record on Drive is unreadable"
    if marker.stage != stage:
        return "its record on Drive belongs to another stage"
    if version is None or marker.code_version != version:
        return "the pipeline code changed since it last ran"
    if marker.config_fingerprint != stage_fingerprint(config, stage):
        return "settings it uses changed"
    for output in marker.outputs:
        out = drive_dir / output.path
        if not out.is_file():
            return f"{output.path} is missing on Drive"
        if out.stat().st_size != output.size_bytes:
            return f"{output.path} on Drive is not the size recorded when it was made"
    if marker.input_fingerprint != input_fingerprint(stage, drive_dir, source):
        return "its input changed"
    return None


@dataclass(frozen=True)
class Plan:
    first_to_run: Optional[str]             # None = every resumable stage is done
    reasons: dict[str, Optional[str]]       # stage -> reason it runs (None = skipped)
    markers: dict[str, StageMarker]         # markers of the stages being skipped

    def runs(self, stage: str) -> bool:
        return self.reasons.get(stage) is not None


def plan(drive_dir: Path, source: Path, config: dict, version: Optional[str],
         force: bool = False) -> Plan:
    reasons: dict[str, Optional[str]] = {}
    first = None
    for stage in RESUMABLE:
        if first is not None:
            reasons[stage] = f"an earlier stage ({STAGE_TITLES[first]}) runs again"
            continue
        reason = "force re-run is ticked" if force else why_not_done(stage, drive_dir, source,
                                                                        config, version)
        reasons[stage] = reason
        if reason is not None:
            first = stage
    skipped = [stage for stage, reason in reasons.items() if reason is None]
    return Plan(first, reasons, {stage: read_marker(drive_dir, stage) for stage in skipped})
