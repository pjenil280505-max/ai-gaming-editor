"""Run the pipeline for one recording: what notebook cells C4 (choose) and C5 (run) call.

Everything the notebook does lives here so it is unit-tested; the notebook
cells stay a few lines long (DEC-013, DEC-024). Resume and the run report
follow DEC-025/026; the notebook/code release check is DEC-027.
"""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core import (VERSION, report, resume, s0_preflight, s1_stage_in, s2_probe, s3_master,
                  s4_analysis, s5_qc)
from core.config import ConfigError, get, load_config, resolve_paths, stage_fingerprint
from core.contracts import ContractError, MediaInfo, QcCheck
from core.errors import StageError
from core.report import RunLog, now
from core.resume import STAGE_TITLES
from core.s0_preflight import human_size
from core.s3_master import Master
from core.s4_analysis import Analysis

TARGET_FPS_CHOICES = ("config", "auto", "30", "60")
D1_LIMIT = 1.5          # docs/PHASE0.md D1: S3 slower than this x real time changes Phase 5

NOTEBOOK_URL = ("https://colab.research.google.com/github/pjenil280505-max/ai-gaming-editor/"
                "blob/v{version}/notebooks/run_pipeline.ipynb")

log_default = functools.partial(print, flush=True)


@dataclass(frozen=True)
class Result:
    match_id: str
    media_info: MediaInfo
    drive_dir: Path
    master: Optional[Master]            # None when S3, S4 and S5 were all skipped
    analysis: Optional[Analysis]        # None when S4 was skipped
    qc: list[QcCheck]                   # S5's checks (from qc.json when S5 was skipped)
    skipped: list[str]                  # stages skipped by the resume rule
    warnings: list[str]
    report_path: Path
    summary_path: Path


def inbox_files(inbox: Path) -> list[Path]:
    """Recordings in the inbox, newest first (hidden files and folders skipped)."""
    files = [p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")]
    return sorted(files, key=lambda p: (-p.stat().st_mtime, p.name))


def inbox_listing(config_path: Optional[Path] = None) -> str:
    """Numbered inbox listing for the C4 form, or a plain-English reason it can't be listed."""
    try:
        paths = resolve_paths(load_config(config_path) if config_path else load_config())
    except ConfigError as exc:
        return str(exc)
    if not paths.inbox.is_dir():
        return f"Inbox folder {paths.inbox} not found. Run cell C1 and check the folder exists."
    files = inbox_files(paths.inbox)
    if not files:
        return f"The inbox {paths.inbox} is empty. Upload a recording there first."
    lines = [f"Recordings in {paths.inbox} (newest first):"]
    lines += [f"  {i}. {p.name}  ({human_size(p.stat().st_size)})" for i, p in enumerate(files, 1)]
    lines.append("Type the number or the file name into the 'recording' field and run this cell again.")
    return "\n".join(lines)


def resolve_choice(choice: str, config_path: Optional[Path] = None) -> str:
    """Turn the form's 'recording' field (a list number or a file name) into a file name."""
    choice = choice.strip()
    if not choice.isdigit():
        return choice
    try:
        inbox = resolve_paths(load_config(config_path) if config_path else load_config()).inbox
        files = inbox_files(inbox) if inbox.is_dir() else []
    except ConfigError:
        files = []
    number = int(choice)
    if 1 <= number <= len(files):
        return files[number - 1].name
    return choice      # S0 reports it as not found


def overrides_for(target_fps: str) -> dict:
    if target_fps not in TARGET_FPS_CHOICES:
        raise ValueError(f"target_fps must be one of {TARGET_FPS_CHOICES}")
    if target_fps == "config":
        return {}
    return {"video.target_fps": target_fps if target_fps == "auto" else int(target_fps)}


def summary(info: MediaInfo) -> str:
    v = info.video
    audio = ", ".join(f"track {t.index}: {t.codec} {t.sample_rate} Hz {t.channels} ch"
                      for t in info.audio_tracks) or "none"
    lines = [
        f"  container {info.container}, {info.duration_s:.1f} s, {human_size(info.size_bytes)}",
        f"  video {v.codec} {v.width}x{v.height}, rotation {v.rotation}°, shown as {v.display_aspect}",
        f"  frame rate {'VARIABLE' if v.is_vfr else 'constant'}: {v.vfr_evidence}",
        f"  master will be {info.target_fps} fps",
        f"  audio {audio}; using "
        + ("none (video-only)" if info.audio_track_index is None else f"track {info.audio_track_index}"),
    ]
    lines += [f"  WARNING: {w}" for w in info.warnings]
    return "\n".join(lines)


def describe_choice(choice: str, config_path: Optional[Path] = None) -> str:
    """C4's answer once a recording is typed in: what was chosen, or why it can't be used."""
    name = resolve_choice(choice, config_path)
    try:
        inbox = resolve_paths(load_config(config_path) if config_path else load_config()).inbox
    except ConfigError as exc:
        return str(exc)
    if name and Path(name).name == name and (inbox / name).is_file():
        return (f"Selected: {name} ({human_size((inbox / name).stat().st_size)}).\n"
                f"Now run cell C5 to process it.")
    return f"'{choice}' is not a recording in the inbox.\n" + inbox_listing(config_path)


def clock(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


class Steps:
    """Calls log once each time progress passes another `step` percent."""

    def __init__(self, log: Callable[[str], None], step: int,
                 describe: Callable[[int, float], str] = lambda pct, _: f"  {pct}%"):
        self.log, self.step, self.describe, self.shown = log, step, describe, 0

    def __call__(self, fraction: float) -> None:
        pct = int(fraction * 100) // self.step * self.step
        if pct > self.shown:
            self.shown = pct
            self.log(self.describe(pct, fraction))


def media_steps(log: Callable[[str], None], duration_s: float, step: int) -> Steps:
    """Progress lines with position, speed (x real time) and time left, for encodes."""
    started = time.monotonic()

    def describe(pct: int, fraction: float) -> str:
        done = fraction * duration_s
        text = f"  {pct}% · {clock(done)} of {clock(duration_s)}"
        if done > 0:
            ratio = (time.monotonic() - started) / done
            text += f" · {ratio:.2f}x real time · about {clock((duration_s - done) * ratio)} left"
        return text

    return Steps(log, step, describe)


def check_notebook(notebook_version: Optional[str]) -> None:
    """Refuse to run with a notebook from a different release than the code (DEC-027)."""
    if notebook_version == VERSION:
        return
    which = "an older release" if notebook_version is None else f"release {notebook_version}"
    raise StageError("Notebook check", [
        f"This notebook is from {which} but the code C2 downloaded is release {VERSION}. "
        f"Open the matching notebook and run C1–C5 there: {NOTEBOOK_URL.format(version=VERSION)}"])


def _status(warnings: list[str]) -> str:
    return "warn" if warnings else "pass"


def run_pipeline(choice: str, target_fps: str = "config", force: bool = False,
                 config_path: Optional[Path] = None,
                 log: Callable[[str], None] = log_default,
                 notebook_version: Optional[str] = VERSION) -> Result:
    """S0 → S5 (+ S6 report) for one inbox recording, skipping stages already done.

    Raises StageError with a plain-English message; once the match is known, a
    failure still writes run_report.json and summary.txt before it is raised.
    """
    check_notebook(notebook_version)
    run = RunLog(log)
    name = resolve_choice(choice, config_path)

    started = now()
    run(f"S0 Preflight: checking Colab, Drive and '{name}' ...")
    pre = s0_preflight.run(name, overrides_for(target_fps), config_path)
    run(f"  {pre.ffmpeg_version}; {pre.cpu_count} CPUs ({pre.cpu_model or 'model unknown'}); "
        f"GPU: {pre.gpu or 'none'}"
        f" (GPU encoder {'works' if pre.gpu_encoder else 'not available'}); "
        f"{human_size(pre.free_disk_bytes)} free on local disk")
    run.done("s0_preflight", started)

    started = now()
    run("S1 Stage-in: identifying the recording ...")
    stage_in = s1_stage_in.identify(pre)
    run(f"  match_id {stage_in.match_id} (time from {stage_in.time_source})")
    drive_dir = pre.paths.work / stage_in.match_id
    version = resume.code_version()
    run_id = f"{run.started:%Y%m%dT%H%M%SZ}"
    plan = resume.plan(drive_dir, pre.source, pre.config, version, force)
    run("Resume check:")
    for stage, reason in plan.reasons.items():
        run(f"  {STAGE_TITLES[stage]}: " + ("done in an earlier run, skipping" if reason is None
                                             else f"runs ({reason})"))
    if version is None:
        run("  (the code version can't be read, so nothing is skipped)")

    media_info: Optional[MediaInfo] = None
    master: Optional[Master] = None
    analysis: Optional[Analysis] = None
    current = "s1_stage_in"
    try:
        if plan.runs("s2_probe") or plan.runs("s3_master"):
            run(f"  copying {human_size(pre.source_size_bytes)} to local disk ...")
            t = time.monotonic()
            stage_in = s1_stage_in.fetch(pre, stage_in, force=force, progress=Steps(run, 10))
            run(f"  {'copied and size verified' if stage_in.copied else 'reused existing local copy (same size)'}"
                f"  [{time.monotonic() - t:.1f} s]")
        else:
            run("  the recording is not needed on local disk this time")
        run.done("s1_stage_in", started)

        current, started = "s2_probe", now()
        if plan.runs("s2_probe"):
            input_fp = resume.input_fingerprint("s2_probe", drive_dir, pre.source)
            resume.clear_marker(drive_dir, "s2_probe")
            run("S2 Probe: measuring the recording ...")
            probe = s2_probe.run(pre, stage_in)
            media_info = probe.media_info
            run(summary(media_info))
            run(f"  saved {probe.drive_path}")
            resume.write_marker(drive_dir, "s2_probe", _status(media_info.warnings), input_fp,
                                stage_fingerprint(pre.config, "s2_probe"), version,
                                [probe.drive_path], started, now())
            run.done("s2_probe", started, media_info.warnings)
        else:
            try:
                media_info = MediaInfo.from_json(
                    (drive_dir / s2_probe.FILE_NAME).read_text(encoding="utf-8"))
            except (ContractError, OSError):
                raise StageError(s2_probe.TITLE, [
                    f"{s2_probe.FILE_NAME} from an earlier run can't be read. Tick force re-run "
                    f"in C4 and run C5 again."]) from None
            probe = s2_probe.Probe(media_info, stage_in.local_dir / s2_probe.FILE_NAME,
                                   drive_dir / s2_probe.FILE_NAME)
            run("S2 Probe: skipped (done in an earlier run)")
            run.skipped("s2_probe", plan.markers["s2_probe"].status, "done in an earlier run",
                        media_info.warnings)

        current, started = "s3_master", now()
        if plan.runs("s3_master"):
            input_fp = resume.input_fingerprint("s3_master", drive_dir, pre.source)
            resume.clear_marker(drive_dir, "s3_master")
            run(f"S3 Master: encoding {clock(media_info.duration_s)} at {media_info.target_fps} fps "
                f"(the slow step; progress every 5%) ...")
            master = s3_master.run(pre, stage_in, probe,
                                   progress=media_steps(run, media_info.duration_s, 5),
                                   copy_progress=Steps(run, 25, lambda pct, _: f"  copied to Drive {pct}%"))
            ratio = master.encode_seconds / master.duration_s if master.duration_s else 0.0
            run(f"  master {master.width}x{master.height}, {master.fps} fps, {clock(master.duration_s)}, "
                f"{human_size(master.size_bytes)}; frame timing checked")
            speed = (f"encoding took {clock(master.encode_seconds)} = {ratio:.2f}x real time "
                     f"({'within' if ratio <= D1_LIMIT else 'OVER'} the D1 limit of {D1_LIMIT}x)")
            run(f"  {speed}")
            run(f"  saved {master.drive_path}")
            for w in master.warnings:
                run(f"  WARNING: {w}")
            resume.write_marker(drive_dir, "s3_master", _status(master.warnings), input_fp,
                                stage_fingerprint(pre.config, "s3_master"), version,
                                [master.drive_path], started, now())
            run.done("s3_master", started, master.warnings, note=f"{ratio:.2f}x real time")
        else:
            run("S3 Master: skipped (done in an earlier run)")
            if plan.runs("s4_analysis"):
                run("  copying the earlier master back from Drive for S4 ...")
                master = s3_master.from_drive(pre, stage_in, media_info, progress=Steps(run, 25))
            run.skipped("s3_master", plan.markers["s3_master"].status, "done in an earlier run")

        current, started = "s4_analysis", now()
        if plan.runs("s4_analysis"):
            input_fp = resume.input_fingerprint("s4_analysis", drive_dir, pre.source)
            resume.clear_marker(drive_dir, "s4_analysis")
            t = time.monotonic()
            run("S4 Analysis copy: making the small video and WAV ...")
            analysis = s4_analysis.run(pre, stage_in, master, progress=Steps(run, 10))
            wav = (f"; analysis.wav {get(pre.config, 'analysis.audio_channels')} ch "
                   f"{get(pre.config, 'analysis.audio_rate')} Hz" if analysis.local_audio else "")
            run(f"  analysis.mp4 {analysis.width}x{analysis.height}, {get(pre.config, 'analysis.fps')} fps"
                f"{wav}  [{time.monotonic() - t:.1f} s]")
            for w in analysis.warnings:
                run(f"  WARNING: {w}")
            outputs = [analysis.drive_video] + ([analysis.drive_audio] if analysis.drive_audio else [])
            resume.write_marker(drive_dir, "s4_analysis", _status(analysis.warnings), input_fp,
                                stage_fingerprint(pre.config, "s4_analysis"), version,
                                outputs, started, now())
            run.done("s4_analysis", started, analysis.warnings)
        else:
            run("S4 Analysis copy: skipped (done in an earlier run)")
            run.skipped("s4_analysis", plan.markers["s4_analysis"].status, "done in an earlier run")

        current, started = "s5_qc", now()
        earlier = None if plan.runs("s5_qc") else s5_qc.load(drive_dir)
        if earlier is None:
            if not plan.runs("s5_qc"):
                run(f"S5 QC: {s5_qc.FILE_NAME} from the earlier run can't be read, so S5 runs again")
            input_fp = resume.input_fingerprint("s5_qc", drive_dir, pre.source)
            resume.clear_marker(drive_dir, "s5_qc")
            t = time.monotonic()
            run("S5 QC: checking the master and the analysis copy ...")
            if master is None:
                run("  copying the earlier master back from Drive for S5 ...")
                master = s3_master.from_drive(pre, stage_in, media_info, progress=Steps(run, 25))
            if analysis is None:
                run("  copying the earlier analysis.mp4 back from Drive for S5 ...")
                analysis_video = s4_analysis.video_from_drive(pre, stage_in, progress=Steps(run, 25))
            else:
                analysis_video = analysis.local_video
            qc = s5_qc.run(pre, stage_in, media_info, master.local_path, analysis_video,
                           progress=Steps(run, 25))
            run.qc = qc.checks
            for line in s5_qc.lines(qc.checks):
                run(line)
            run(f"  saved {qc.drive_path}  [{time.monotonic() - t:.1f} s]")
            if qc.failures:
                raise StageError(s5_qc.TITLE, qc.failures)
            resume.write_marker(drive_dir, "s5_qc", _status(qc.warnings), input_fp,
                                stage_fingerprint(pre.config, "s5_qc"), version,
                                [qc.drive_path], started, now())
            run.done("s5_qc", started, qc.warnings)
        else:
            run.qc = earlier
            run("S5 QC: skipped (done in an earlier run); its results:")
            for line in s5_qc.lines(earlier):
                run(line)
            run.skipped("s5_qc", plan.markers["s5_qc"].status, "done in an earlier run",
                        s5_qc.outcome(earlier)[0])
    except StageError as err:
        run.failed(current, started, err.problems)
        run(str(err))
        try:
            report.write(run, run_id, stage_in.match_id, version, pre, media_info,
                         stage_in.local_dir, drive_dir)
            run(f"The run report is in {drive_dir}.")
        except StageError as report_err:
            run(str(report_err))
        raise

    run("S6 Stage-out: writing the run report ...")
    report_path, summary_path = report.write(run, run_id, stage_in.match_id, version, pre,
                                             media_info, stage_in.local_dir, drive_dir)
    skipped = [r.name for r in run.records if r.skipped]
    warnings = [w for r in run.records for w in r.warnings]
    run(f"\nDone. Match {stage_in.match_id} is in {drive_dir}")
    run(f"  {report.WATCH_HINT}")
    run(f"  Run summary: {summary_path.name}; details: {report_path.name}")
    return Result(stage_in.match_id, media_info, drive_dir, master, analysis, run.qc, skipped,
                  warnings, report_path, summary_path)
