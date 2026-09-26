"""Phase 0 contracts: media_info.json, stage markers, run_report.json.

The dataclasses below are the single source of truth. Validation and the
JSON Schema mirrors in schemas/ are both derived from the field types and the
per-field metadata (description, enum, minimum, pattern, format), so the three
cannot drift apart. Regenerate the mirrors after any contract change with

    python -m core.contracts

and record the change in docs/DECISIONS.md (CLAUDE.md rule 8).
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import sys
import types
import typing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Pipeline stage ids, in run order (docs/PHASE0.md §Stages).
STAGES = (
    "s0_preflight",
    "s1_stage_in",
    "s2_probe",
    "s3_master",
    "s4_analysis",
    "s5_qc",
    "s6_stage_out",
)

# One status vocabulary for stages and QC checks. A stage marker is written
# only after a stage completes, so a marker is never "fail".
STATUSES = ("pass", "warn", "fail")

SHA256_HEX = r"^[0-9a-f]{64}$"
MATCH_ID = r"^[0-9]{8}-[0-9]{4}-[0-9a-f]{6}$"
FRAME_RATE = r"^[0-9]+/[0-9]+$"
ASPECT = r"^[0-9]+:[0-9]+$"
# Relative POSIX path: no leading "/", no backslash, no ".." segment.
REL_PATH = r"^(?!/)(?!.*\\)(?!(.*/)?\.\.(/|$)).+$"

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


class ContractError(ValueError):
    """Raised when data does not satisfy a contract; lists every problem."""

    def __init__(self, contract: str, problems: list[str]):
        self.contract = contract
        self.problems = list(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"{contract} is invalid ({len(self.problems)} problem(s)):\n{lines}")


def _f(description: str, **rules: Any):
    """A required dataclass field carrying its description and validation rules."""
    return field(metadata={"description": description, **rules})


class _Contract:
    """Shared behaviour for top-level contract dataclasses."""

    CONTRACT_NAME = ""
    CONTRACT_FILE = ""
    CONTRACT_DESCRIPTION = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"

    def validate(self) -> list[str]:
        """Return every problem found (empty list = valid)."""
        return validate_data(type(self), self.to_dict())

    @classmethod
    def from_dict(cls, data: Any):
        problems = validate_data(cls, data)
        if problems:
            raise ContractError(cls.CONTRACT_NAME, problems)
        return _build(cls, data)

    @classmethod
    def from_json(cls, text: str):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ContractError(cls.CONTRACT_NAME, [f"not valid JSON: {exc}"]) from None
        return cls.from_dict(data)

    @classmethod
    def _cross_checks(cls, data: dict) -> list[str]:
        return []


# --------------------------------------------------------------------------
# media_info.json (written by S2 Probe)


@dataclass
class FrameDurationStats:
    min: float = _f("Shortest frame duration, seconds.", minimum=0)
    median: float = _f("Median frame duration, seconds.", minimum=0)
    max: float = _f("Longest frame duration, seconds.", minimum=0)
    stdev: float = _f("Standard deviation of frame durations, seconds.", minimum=0)


@dataclass
class VideoInfo:
    codec: str = _f("Video codec name as reported by ffprobe (e.g. h264, hevc).", min_length=1)
    width: int = _f("Coded width in pixels, before rotation.", minimum=1)
    height: int = _f("Coded height in pixels, before rotation.", minimum=1)
    rotation: int = _f(
        "Clockwise rotation in degrees a player applies for display "
        "(the classic 'rotate' tag convention).",
        enum=[0, 90, 180, 270],
    )
    display_aspect: str = _f(
        "Displayed aspect ratio after sample aspect ratio and rotation, reduced (e.g. 20:9).",
        pattern=ASPECT,
    )
    r_frame_rate: str = _f("ffprobe r_frame_rate as a fraction (e.g. 30/1).", pattern=FRAME_RATE)
    avg_frame_rate: str = _f("ffprobe avg_frame_rate as a fraction.", pattern=FRAME_RATE)
    frame_durations_s: FrameDurationStats = _f("Frame-duration statistics from packet timestamps.")
    is_vfr: bool = _f("True when frame durations vary (variable frame rate).")
    vfr_evidence: str = _f("Plain-English reason for the is_vfr verdict, with the numbers behind it.")


@dataclass
class AudioTrack:
    index: int = _f(
        "Position among the file's audio streams (0 = first audio stream); "
        "the value audio.track_index refers to.",
        minimum=0,
    )
    codec: str = _f("Audio codec name as reported by ffprobe.", min_length=1)
    sample_rate: int = _f("Sample rate in Hz.", minimum=1)
    channels: int = _f("Channel count.", minimum=1)
    start_offset_s: float = _f("Audio stream start time minus video stream start time, seconds.")


@dataclass
class MediaInfo(_Contract):
    container: str = _f("Container format name as reported by ffprobe.", min_length=1)
    duration_s: float = _f("Container duration, seconds.", minimum=0)
    size_bytes: int = _f("File size in bytes.", minimum=0)
    video: VideoInfo = _f("The first video stream.")
    audio_tracks: list[AudioTrack] = _f("Every audio stream in the file; empty when there is none.")
    target_fps: int = _f("Frame rate the master will be encoded at.", enum=[30, 60])
    audio_track_index: Optional[int] = _f(
        "AudioTrack.index chosen for the master; null = video-only mode.", minimum=0
    )
    warnings: list[str] = _f("Plain-English warnings for the owner.")

    CONTRACT_NAME = "media_info"
    CONTRACT_FILE = "media_info.schema.json"
    CONTRACT_DESCRIPTION = "Probe results for one source recording (docs/PHASE0.md S2)."

    @classmethod
    def _cross_checks(cls, data: dict) -> list[str]:
        problems = []
        tracks = data.get("audio_tracks")
        chosen = data.get("audio_track_index")
        if isinstance(tracks, list) and all(isinstance(t, dict) for t in tracks):
            indexes = [t.get("index") for t in tracks]
            ints = [i for i in indexes if isinstance(i, int) and not isinstance(i, bool)]
            if len(ints) != len(set(ints)):
                problems.append("audio_tracks: index values must be unique")
            if tracks and chosen is None:
                problems.append("audio_track_index: must name a track when audio_tracks is not empty")
            if isinstance(chosen, int) and not isinstance(chosen, bool) and chosen not in indexes:
                problems.append(
                    f"audio_track_index: {chosen} is not the index of any entry in audio_tracks"
                )
        return problems


# --------------------------------------------------------------------------
# stages/<stage>.done.json


@dataclass
class OutputFile:
    path: str = _f("Output path relative to the match work dir, '/'-separated.", pattern=REL_PATH)
    size_bytes: int = _f("Size in bytes when the stage finished.", minimum=0)


@dataclass
class StageMarker(_Contract):
    stage: str = _f("Stage id. S0 always runs, so it never has a marker (DEC-010).",
                    enum=list(STAGES[1:]))
    status: str = _f(
        "Outcome of the completed stage (markers are written only after success).",
        enum=["pass", "warn"],
    )
    input_fingerprint: str = _f("SHA-256 (hex) of the stage's inputs.", pattern=SHA256_HEX)
    config_fingerprint: str = _f(
        "SHA-256 (hex) of the config keys this stage uses (core.config.stage_fingerprint).",
        pattern=SHA256_HEX,
    )
    code_version: str = _f("Git commit SHA of the pipeline code.", min_length=1)
    outputs: list[OutputFile] = _f("Every file the stage produced.")
    started_at: str = _f("ISO 8601 timestamp with timezone.", format="date-time")
    finished_at: str = _f("ISO 8601 timestamp with timezone.", format="date-time")

    CONTRACT_NAME = "stage_marker"
    CONTRACT_FILE = "stage_marker.schema.json"
    CONTRACT_DESCRIPTION = (
        "Written after a stage's outputs are complete; drives the resume rule (docs/PHASE0.md)."
    )

    @classmethod
    def _cross_checks(cls, data: dict) -> list[str]:
        return _time_order(data, "")


# --------------------------------------------------------------------------
# run_report.json


@dataclass
class Environment:
    platform: str = _f("Platform string (e.g. from platform.platform()).", min_length=1)
    cpu_count: int = _f("Logical CPU count.", minimum=1)
    gpu: Optional[str] = _f("GPU name; null when no GPU.")
    ffmpeg_version: str = _f("ffmpeg version string.", min_length=1)


@dataclass
class StageResult:
    name: str = _f("Stage id.", enum=list(STAGES))
    status: str = _f("Stage outcome.", enum=list(STATUSES))
    started_at: str = _f("ISO 8601 timestamp with timezone.", format="date-time")
    finished_at: str = _f("ISO 8601 timestamp with timezone.", format="date-time")
    duration_s: float = _f("Wall-clock seconds.", minimum=0)
    skipped: bool = _f("True when the resume rule skipped this stage.")
    warnings: list[str] = _f("Plain-English warnings.")
    errors: list[str] = _f("Plain-English errors.")


@dataclass
class QcCheck:
    check: str = _f("QC check name.", min_length=1)
    status: str = _f("Check outcome.", enum=list(STATUSES))
    value: Any = _f("Measured value (number, text, list of spans, or null).")
    threshold: Any = _f("Threshold the value was compared with, or null.")


@dataclass
class Totals:
    footage_minutes: float = _f("Source duration, minutes.", minimum=0)
    processing_minutes: float = _f("Total wall-clock processing time, minutes.", minimum=0)
    minutes_per_footage_minute: Optional[float] = _f(
        "processing_minutes / footage_minutes; null when footage_minutes is 0.", minimum=0
    )


@dataclass
class RunReport(_Contract):
    run_id: str = _f("Unique id of this run.", min_length=1)
    match_id: str = _f(
        "YYYYMMDD-HHMM (file mtime) + '-' + 6 hex chars of the content hash.", pattern=MATCH_ID
    )
    code_version: str = _f("Git commit SHA of the pipeline code.", min_length=1)
    config_fingerprint: str = _f(
        "SHA-256 (hex) of every content-affecting config key (core.config.config_fingerprint).",
        pattern=SHA256_HEX,
    )
    environment: Environment = _f("Where the run happened.")
    stages: list[StageResult] = _f("One entry per stage attempted or skipped, in run order.")
    qc: list[QcCheck] = _f("QC results (S5).")
    totals: Totals = _f("Run totals.")

    CONTRACT_NAME = "run_report"
    CONTRACT_FILE = "run_report.schema.json"
    CONTRACT_DESCRIPTION = "Summary of one pipeline run for one match (docs/PHASE0.md S6)."

    @classmethod
    def _cross_checks(cls, data: dict) -> list[str]:
        problems = []
        stages = data.get("stages")
        if isinstance(stages, list):
            for i, stage in enumerate(stages):
                if isinstance(stage, dict):
                    problems += _time_order(stage, f"stages[{i}].")
        return problems


CONTRACTS = (MediaInfo, StageMarker, RunReport)


# --------------------------------------------------------------------------
# Validation (derived from the dataclasses above)


def _parse_time(value: str) -> Optional[datetime]:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _time_order(data: dict, prefix: str) -> list[str]:
    start, end = data.get("started_at"), data.get("finished_at")
    if isinstance(start, str) and isinstance(end, str):
        t0, t1 = _parse_time(start), _parse_time(end)
        if t0 and t1 and t1 < t0:
            return [f"{prefix}finished_at: {end} is before started_at {start}"]
    return []


def _unwrap_optional(tp: Any) -> tuple[Any, bool]:
    if typing.get_origin(tp) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) != 1:
            raise TypeError(f"unsupported union type {tp!r}")
        return args[0], True
    return tp, False


def _type_name(value: Any) -> str:
    names = {bool: "true/false", int: "integer", float: "number", str: "text",
             list: "list", dict: "object", type(None): "null"}
    return names.get(type(value), type(value).__name__)


def _check_value(value: Any, tp: Any, rules: dict, path: str, problems: list[str]) -> None:
    tp, nullable = _unwrap_optional(tp)
    if tp is Any:
        return
    if value is None:
        if not nullable:
            problems.append(f"{path}: must not be null")
        return
    if dataclasses.is_dataclass(tp):
        if not isinstance(value, dict):
            problems.append(f"{path}: expected an object, got {_type_name(value)}")
        else:
            _check_object(value, tp, path + ".", problems)
        return
    if typing.get_origin(tp) is list:
        if not isinstance(value, list):
            problems.append(f"{path}: expected a list, got {_type_name(value)}")
            return
        (item_tp,) = typing.get_args(tp)
        for i, item in enumerate(value):
            _check_value(item, item_tp, {}, f"{path}[{i}]", problems)
        return

    if tp is bool:
        ok = isinstance(value, bool)
    elif tp is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif tp is float:
        ok = (isinstance(value, (int, float)) and not isinstance(value, bool)
              and math.isfinite(value))
    elif tp is str:
        ok = isinstance(value, str)
    else:
        raise TypeError(f"unsupported field type {tp!r}")
    if not ok:
        expected = {bool: "true/false", int: "integer", float: "finite number", str: "text"}[tp]
        problems.append(f"{path}: expected {expected}, got {_type_name(value)} {value!r}")
        return

    if "enum" in rules and value not in rules["enum"]:
        problems.append(f"{path}: must be one of {rules['enum']}, got {value!r}")
    if "minimum" in rules and value < rules["minimum"]:
        problems.append(f"{path}: must be >= {rules['minimum']}, got {value!r}")
    if "min_length" in rules and len(value) < rules["min_length"]:
        problems.append(f"{path}: must not be empty")
    if "pattern" in rules and not re.search(rules["pattern"], value):
        problems.append(f"{path}: {value!r} does not match pattern {rules['pattern']}")
    if rules.get("format") == "date-time" and _parse_time(value) is None:
        problems.append(f"{path}: {value!r} is not an ISO 8601 timestamp with timezone")


def _check_object(data: dict, cls: type, prefix: str, problems: list[str]) -> None:
    hints = typing.get_type_hints(cls)
    fields = dataclasses.fields(cls)
    for f in fields:
        if f.name not in data:
            problems.append(f"{prefix}{f.name}: missing")
        else:
            _check_value(data[f.name], hints[f.name], f.metadata, prefix + f.name, problems)
    known = {f.name for f in fields}
    for key in data:
        if key not in known:
            problems.append(f"{prefix}{key}: unexpected field")


def validate_data(cls: type, data: Any) -> list[str]:
    """Validate JSON-like data against contract `cls`; return every problem found."""
    if not isinstance(data, dict):
        return [f"expected a JSON object at the top level, got {_type_name(data)}"]
    problems: list[str] = []
    _check_object(data, cls, "", problems)
    problems += cls._cross_checks(data)
    return problems


def _build(tp: Any, value: Any) -> Any:
    tp, _ = _unwrap_optional(tp)
    if value is None or tp is Any:
        return value
    if dataclasses.is_dataclass(tp):
        hints = typing.get_type_hints(tp)
        return tp(**{f.name: _build(hints[f.name], value[f.name]) for f in dataclasses.fields(tp)})
    if typing.get_origin(tp) is list:
        (item_tp,) = typing.get_args(tp)
        return [_build(item_tp, item) for item in value]
    if tp is float:
        return float(value)
    return value


# --------------------------------------------------------------------------
# JSON Schema mirrors (human-readable; generated, never hand-edited)


def _schema_for(tp: Any, rules: dict) -> dict:
    tp, nullable = _unwrap_optional(tp)
    schema: dict[str, Any] = {}
    if rules.get("description"):
        schema["description"] = rules["description"]
    if tp is Any:
        return schema
    if dataclasses.is_dataclass(tp):
        schema.update(_object_schema(tp))
    elif typing.get_origin(tp) is list:
        (item_tp,) = typing.get_args(tp)
        schema["type"] = "array"
        schema["items"] = _schema_for(item_tp, {})
    else:
        schema["type"] = {bool: "boolean", int: "integer", float: "number", str: "string"}[tp]
        for rule, key in (("enum", "enum"), ("minimum", "minimum"), ("min_length", "minLength"),
                          ("pattern", "pattern"), ("format", "format")):
            if rule in rules:
                schema[key] = rules[rule]
    if nullable:
        if isinstance(schema.get("type"), str):
            schema["type"] = [schema["type"], "null"]
            if "enum" in schema:
                schema["enum"] = schema["enum"] + [None]
        else:
            inner = {k: v for k, v in schema.items() if k != "description"}
            schema = {k: v for k, v in schema.items() if k == "description"}
            schema["anyOf"] = [inner, {"type": "null"}]
    return schema


def _object_schema(cls: type) -> dict:
    hints = typing.get_type_hints(cls)
    fields = dataclasses.fields(cls)
    return {
        "type": "object",
        "properties": {f.name: _schema_for(hints[f.name], dict(f.metadata)) for f in fields},
        "required": [f.name for f in fields],
        "additionalProperties": False,
    }


def json_schema(cls: type) -> dict:
    """JSON Schema (draft 2020-12) mirror of a top-level contract."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": cls.CONTRACT_NAME,
        "description": cls.CONTRACT_DESCRIPTION
        + " Generated from core/contracts.py by `python -m core.contracts`; do not edit by hand.",
    }
    schema.update(_object_schema(cls))
    return schema


def schema_text(cls: type) -> str:
    return json.dumps(json_schema(cls), indent=2) + "\n"


def write_schemas(directory: Path = SCHEMA_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for cls in CONTRACTS:
        path = directory / cls.CONTRACT_FILE
        path.write_text(schema_text(cls), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for written_path in write_schemas():
        print(f"wrote {written_path}", file=sys.stderr)
