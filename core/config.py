"""Load and validate configs/pipeline.yaml; compute config fingerprints.

load_config() rejects the whole file with one ConfigError that lists every
missing, unknown or out-of-range key, so the owner can fix them in one edit.

Fingerprints (docs/PHASE0.md §Config) hash only the keys that change what a
stage writes. paths.* are deliberately excluded: they decide where files live,
not what is in them, so moving the Drive folder does not force a re-run.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from core.contracts import STAGES

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "pipeline.yaml"

X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow", "placebo")

FINGERPRINT_SCHEME = "config-fingerprint-v1"


class ConfigError(ValueError):
    """Raised for an unusable config; `problems` lists every issue found."""

    def __init__(self, problems: list[str], source: Optional[str] = None):
        self.problems = list(problems)
        self.source = source
        where = f" ({source})" if source else ""
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"Pipeline config is invalid{where}:\n{lines}")


# ---- value checks: each returns None when the value is acceptable ----------

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)) and math.isfinite(v)


def _int_range(lo: int, hi: Optional[int] = None, even: bool = False) -> Callable[[Any], bool]:
    return lambda v: _is_int(v) and v >= lo and (hi is None or v <= hi) and (not even or v % 2 == 0)


def _number_range(lo: float, hi: Optional[float] = None, lo_inclusive: bool = True):
    def check(v: Any) -> bool:
        if not _is_number(v):
            return False
        above = v >= lo if lo_inclusive else v > lo
        return above and (hi is None or v <= hi)
    return check


def _one_of(*allowed: Any) -> Callable[[Any], bool]:
    # type() comparison keeps True from matching 1 and "30" from matching 30
    return lambda v: any(type(v) is type(a) and v == a for a in allowed)


def _abs_path(v: Any) -> bool:
    return isinstance(v, str) and posixpath.isabs(v) and "\\" not in v


def _path(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != "" and "\\" not in v


# key -> (check, what the owner should write)
SPEC: dict[str, tuple[Callable[[Any], bool], str]] = {
    "paths.drive_root": (_abs_path, "an absolute path such as /content/drive/MyDrive/AIEditor"),
    "paths.inbox": (_path, "a path; relative paths are inside drive_root"),
    "paths.work": (_path, "a path; relative paths are inside drive_root"),
    "paths.local_work": (_abs_path, "an absolute path such as /content/aieditor_work"),
    "video.target_fps": (_one_of("auto", 30, 60), "auto, 30 or 60"),
    "video.master_codec": (_one_of("h264"), "h264 (the only codec supported in Phase 0)"),
    "video.master_crf": (_int_range(0, 51), "a whole number from 0 to 51"),
    "video.master_preset": (_one_of(*X264_PRESETS), "one of " + ", ".join(X264_PRESETS)),
    "analysis.width": (_int_range(16, even=True), "an even whole number, at least 16"),
    "analysis.fps": (_int_range(1, 60), "a whole number from 1 to 60"),
    "analysis.audio_rate": (_int_range(8000, 192000), "a whole number of Hz from 8000 to 192000"),
    "analysis.audio_channels": (_one_of(1, 2), "1 or 2"),
    "audio.track_index": (_int_range(0), "a whole number, 0 or more (0 = first audio track)"),
    "audio.master_sample_rate": (_int_range(8000, 192000), "a whole number of Hz from 8000 to 192000"),
    "qc.duration_tolerance_s": (_number_range(0, lo_inclusive=False), "a number of seconds greater than 0"),
    "qc.av_sync_tolerance_frames": (_number_range(0), "a number of frames, 0 or more"),
    "loudness.target_lufs": (_number_range(-70, 0), "a number of LUFS from -70 to 0"),
}

SECTIONS = tuple(dict.fromkeys(key.split(".")[0] for key in SPEC))

# Config keys whose values change what each stage writes (see module docstring).
# S2 records the chosen target_fps and audio track in media_info.json, so S3
# sees those two keys through its input fingerprint rather than directly.
STAGE_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "s0_preflight": (),
    "s1_stage_in": (),
    "s2_probe": ("video.target_fps", "audio.track_index"),
    "s3_master": ("video.master_codec", "video.master_crf", "video.master_preset",
                  "audio.master_sample_rate"),
    "s4_analysis": ("analysis.width", "analysis.fps", "analysis.audio_rate",
                    "analysis.audio_channels"),
    "s5_qc": ("qc.duration_tolerance_s", "qc.av_sync_tolerance_frames"),
    "s6_stage_out": (),
}

CONTENT_KEYS = tuple(key for key in SPEC if not key.startswith("paths."))


def _describe(v: Any) -> str:
    if isinstance(v, str):
        return f"'{v}' (text)"
    if isinstance(v, bool):
        return f"{str(v).lower()} (true/false)"
    if v is None:
        return "nothing"
    return repr(v)


def validate_config(data: Any) -> list[str]:
    """Return every problem with a parsed config (empty list = valid)."""
    if data is None:
        return ["the config file is empty"]
    if not isinstance(data, dict):
        return [f"the top level must be sections of keys, got {_describe(data)}"]

    problems = []
    for section in data:
        if section not in SECTIONS:
            problems.append(f"{section}: unknown section (expected one of {', '.join(SECTIONS)})")
    for section in SECTIONS:
        if section not in data:
            problems.append(f"{section}: missing section")
            continue
        values = data[section]
        if not isinstance(values, dict):
            problems.append(f"{section}: must be a section of keys, got {_describe(values)}")
            continue
        section_keys = [k for k in SPEC if k.startswith(section + ".")]
        for key in section_keys:
            name = key.split(".", 1)[1]
            check, expected = SPEC[key]
            if name not in values:
                problems.append(f"{key}: missing (expected {expected})")
            elif not check(values[name]):
                problems.append(f"{key}: expected {expected}, got {_describe(values[name])}")
        for name in values:
            if f"{section}.{name}" not in SPEC:
                problems.append(f"{section}.{name}: unknown key (typo?)")
    return problems


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict:
    """Read and validate a pipeline YAML file; raise ConfigError listing every problem."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(["file not found"], str(path)) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError([f"not valid YAML: {exc}"], str(path)) from None
    problems = validate_config(data)
    if problems:
        raise ConfigError(problems, str(path))
    return data


def get(config: dict, key: str) -> Any:
    """Look up a dotted key such as 'video.master_crf'."""
    section, name = key.split(".", 1)
    return config[section][name]


def _canonical(value: Any) -> Any:
    # 1 and 1.0 mean the same setting, so they must hash the same.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def config_fingerprint(config: dict, keys: Optional[tuple[str, ...]] = None) -> str:
    """SHA-256 hex of the given keys' values (default: every content-affecting key)."""
    keys = CONTENT_KEYS if keys is None else keys
    unknown = [k for k in keys if k not in SPEC]
    if unknown:
        raise KeyError(f"not config keys: {unknown}")
    payload = {k: _canonical(get(config, k)) for k in sorted(keys)}
    text = json.dumps([FINGERPRINT_SCHEME, payload], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stage_fingerprint(config: dict, stage: str) -> str:
    """Config fingerprint for one stage: hash of only the keys that stage uses."""
    if stage not in STAGE_CONFIG_KEYS:
        raise KeyError(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")
    return config_fingerprint(config, STAGE_CONFIG_KEYS[stage])

