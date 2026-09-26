"""configs/pipeline.yaml loading, validation and fingerprints (core/config.py)."""

from __future__ import annotations

import copy
import math
import tempfile
import unittest
from pathlib import Path

import yaml

from core import config
from core.config import ConfigError, load_config, stage_fingerprint, validate_config
from core.contracts import STAGES

# Defaults exactly as written in docs/PHASE0.md §Config (paths: proposed defaults).
PHASE0_DEFAULTS = {
    "paths": {"drive_root": "/content/drive/MyDrive/AIEditor", "inbox": "inbox",
              "work": "work", "local_work": "/content/aieditor_work"},
    "video": {"target_fps": "auto", "master_codec": "h264", "master_crf": 18,
              "master_preset": "veryfast"},
    "analysis": {"width": 640, "fps": 10, "audio_rate": 16000, "audio_channels": 1},
    "audio": {"track_index": 0, "master_sample_rate": 48000},
    "qc": {"duration_tolerance_s": 0.1, "av_sync_tolerance_frames": 1},
    "loudness": {"target_lufs": -14},
}

# A different but valid value for every key.
ALTERNATIVES = {
    "paths.drive_root": "/content/drive/MyDrive/Other",
    "paths.inbox": "/content/drive/MyDrive/uploads",
    "paths.work": "work2",
    "paths.local_work": "/tmp/aieditor",
    "video.target_fps": 60,
    "video.master_codec": "h264",   # only one allowed value; excluded from change tests
    "video.master_crf": 20,
    "video.master_preset": "medium",
    "analysis.width": 854,
    "analysis.fps": 5,
    "analysis.audio_rate": 22050,
    "analysis.audio_channels": 2,
    "audio.track_index": 1,
    "audio.master_sample_rate": 44100,
    "qc.duration_tolerance_s": 0.2,
    "qc.av_sync_tolerance_frames": 2,
    "loudness.target_lufs": -16,
}

BAD_VALUES = {
    "paths.drive_root": ["AIEditor", "", "C:\\AIEditor", 5, None],
    "paths.inbox": ["", "   ", None, 3],
    "paths.work": ["", None],
    "paths.local_work": ["work", None],
    "video.target_fps": [25, "30", "Auto", True, 30.0, None],
    "video.master_codec": ["hevc", "H264", None],
    "video.master_crf": [-1, 52, 18.5, "18", True, None],
    "video.master_preset": ["fastest", "", None],
    "analysis.width": [0, 8, 641, 640.0, "640", None],
    "analysis.fps": [0, 61, 10.5, None],
    "analysis.audio_rate": [7999, 200000, "16k", None],
    "analysis.audio_channels": [0, 3, "mono", None],
    "audio.track_index": [-1, 0.5, "first", False, None],
    "audio.master_sample_rate": [0, 44.1, None],
    "qc.duration_tolerance_s": [0, -0.1, "0.1", math.nan, math.inf, None],
    "qc.av_sync_tolerance_frames": [-1, "one", None],
    "loudness.target_lufs": [1, -71, "-14", None],
}


def defaults() -> dict:
    return copy.deepcopy(PHASE0_DEFAULTS)


def with_value(cfg: dict, key: str, value) -> dict:
    changed = copy.deepcopy(cfg)
    section, name = key.split(".")
    changed[section][name] = value
    return changed


def write_yaml(directory: str, data) -> Path:
    path = Path(directory) / "pipeline.yaml"
    path.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


class LoadTest(unittest.TestCase):

    def test_repo_defaults_load_and_match_phase0(self):
        self.assertEqual(load_config(), PHASE0_DEFAULTS)

    def test_default_path_is_repo_file(self):
        repo = Path(__file__).resolve().parent.parent
        self.assertEqual(config.DEFAULT_CONFIG_PATH, repo / "configs" / "pipeline.yaml")

    def test_spec_covers_exactly_the_phase0_keys(self):
        keys = {f"{s}.{k}" for s, section in PHASE0_DEFAULTS.items() for k in section}
        self.assertEqual(set(config.SPEC), keys)

    def test_load_from_other_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = with_value(defaults(), "video.target_fps", 30)
            self.assertEqual(load_config(write_yaml(tmp, cfg))["video"]["target_fps"], 30)

    def test_missing_file(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config("/nonexistent/pipeline.yaml")
        self.assertIn("file not found", str(ctx.exception))

    def test_yaml_syntax_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError) as ctx:
                load_config(write_yaml(tmp, "video:\n  master_crf: [18\n"))
            self.assertIn("not valid YAML", str(ctx.exception))

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError) as ctx:
                load_config(write_yaml(tmp, ""))
            self.assertEqual(ctx.exception.problems, ["the config file is empty"])

    def test_error_message_lists_every_problem(self):
        cfg = with_value(with_value(defaults(), "video.master_crf", 99), "analysis.fps", 0)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError) as ctx:
                load_config(write_yaml(tmp, cfg))
        message = str(ctx.exception)
        self.assertEqual(len(ctx.exception.problems), 2)
        self.assertIn("video.master_crf: expected a whole number from 0 to 51, got 99", message)
        self.assertIn("analysis.fps", message)


class ValidateTest(unittest.TestCase):

    def test_defaults_valid(self):
        self.assertEqual(validate_config(defaults()), [])

    def test_alternative_values_valid(self):
        for key, value in ALTERNATIVES.items():
            with self.subTest(key=key):
                self.assertEqual(validate_config(with_value(defaults(), key, value)), [])

    def test_other_valid_values(self):
        for key, value in [("video.target_fps", 30), ("video.master_crf", 0),
                           ("video.master_crf", 51), ("qc.duration_tolerance_s", 1),
                           ("qc.av_sync_tolerance_frames", 0.5), ("loudness.target_lufs", -23.5)]:
            with self.subTest(key=key, value=value):
                self.assertEqual(validate_config(with_value(defaults(), key, value)), [])

    def test_each_bad_value_rejected_with_its_key_named(self):
        self.assertEqual(set(BAD_VALUES), set(config.SPEC))
        for key, values in BAD_VALUES.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    problems = validate_config(with_value(defaults(), key, value))
                    self.assertEqual(len(problems), 1, problems)
                    self.assertTrue(problems[0].startswith(f"{key}: expected "), problems)

    def test_all_bad_values_reported_together(self):
        cfg = defaults()
        for key, values in BAD_VALUES.items():
            cfg = with_value(cfg, key, values[0])
        problems = validate_config(cfg)
        self.assertEqual(len(problems), len(BAD_VALUES))
        for key in BAD_VALUES:
            self.assertTrue(any(p.startswith(key + ":") for p in problems), key)

    def test_missing_keys_each_reported(self):
        cfg = defaults()
        del cfg["video"]["master_crf"]
        del cfg["qc"]["duration_tolerance_s"]
        self.assertEqual(validate_config(cfg), [
            "video.master_crf: missing (expected a whole number from 0 to 51)",
            "qc.duration_tolerance_s: missing (expected a number of seconds greater than 0)",
        ])

    def test_missing_section(self):
        cfg = defaults()
        del cfg["loudness"]
        self.assertEqual(validate_config(cfg), ["loudness: missing section"])

    def test_section_not_a_mapping(self):
        cfg = defaults()
        cfg["analysis"] = 640
        self.assertEqual(validate_config(cfg), ["analysis: must be a section of keys, got 640"])

    def test_unknown_key_and_section(self):
        cfg = defaults()
        cfg["video"]["target_fsp"] = 30
        cfg["extras"] = {}
        problems = validate_config(cfg)
        self.assertIn("video.target_fsp: unknown key (typo?)", problems)
        self.assertTrue(any(p.startswith("extras: unknown section") for p in problems), problems)
        self.assertEqual(len(problems), 2)

    def test_top_level_not_a_mapping(self):
        self.assertEqual(len(validate_config(["video"])), 1)
        self.assertEqual(validate_config(None), ["the config file is empty"])


class FingerprintTest(unittest.TestCase):

    def test_is_sha256_hex(self):
        for stage in STAGES:
            fp = stage_fingerprint(defaults(), stage)
            self.assertRegex(fp, r"^[0-9a-f]{64}$")

    def test_stable_across_calls_reloads_and_key_order(self):
        cfg = defaults()
        reordered = {s: dict(reversed(list(v.items()))) for s, v in reversed(list(cfg.items()))}
        loaded = load_config()
        for stage in STAGES:
            with self.subTest(stage=stage):
                fp = stage_fingerprint(cfg, stage)
                self.assertEqual(fp, stage_fingerprint(cfg, stage))
                self.assertEqual(fp, stage_fingerprint(reordered, stage))
                self.assertEqual(fp, stage_fingerprint(loaded, stage))

    def test_known_value_for_defaults(self):
        # Changing the fingerprint scheme invalidates every stage marker on
        # Drive (full re-runs), so it must be a deliberate, visible change.
        self.assertEqual(stage_fingerprint(defaults(), "s3_master"),
                         "174680f6cc468bd6a27b915d430361014c5110cdf218123de3d421b011f137f5")

    def test_changes_when_a_used_key_changes_and_only_then(self):
        base = defaults()
        for stage in STAGES:
            used = config.STAGE_CONFIG_KEYS[stage]
            for key, value in ALTERNATIVES.items():
                if value == config.get(base, key):
                    continue
                with self.subTest(stage=stage, key=key):
                    changed = stage_fingerprint(with_value(base, key, value), stage)
                    if key in used:
                        self.assertNotEqual(changed, stage_fingerprint(base, stage))
                    else:
                        self.assertEqual(changed, stage_fingerprint(base, stage))

    def test_stage_keys_cover_all_stages_and_real_keys(self):
        self.assertEqual(tuple(config.STAGE_CONFIG_KEYS), STAGES)
        for keys in config.STAGE_CONFIG_KEYS.values():
            for key in keys:
                self.assertIn(key, config.SPEC)

    def test_every_content_key_used_except_render_only_loudness(self):
        used = {k for keys in config.STAGE_CONFIG_KEYS.values() for k in keys}
        unused = set(config.CONTENT_KEYS) - used
        self.assertEqual(unused, {"loudness.target_lufs"})

    def test_paths_never_affect_fingerprints(self):
        moved = defaults()
        for key in ("paths.drive_root", "paths.inbox", "paths.work", "paths.local_work"):
            moved = with_value(moved, key, ALTERNATIVES[key])
        for stage in STAGES:
            self.assertEqual(stage_fingerprint(moved, stage), stage_fingerprint(defaults(), stage))
        self.assertEqual(config.config_fingerprint(moved), config.config_fingerprint(defaults()))

    def test_int_and_float_spelling_hash_the_same(self):
        cfg = with_value(defaults(), "qc.av_sync_tolerance_frames", 1.0)
        self.assertEqual(stage_fingerprint(cfg, "s5_qc"), stage_fingerprint(defaults(), "s5_qc"))

    def test_run_fingerprint_covers_every_content_key(self):
        base = config.config_fingerprint(defaults())
        for key in config.CONTENT_KEYS:
            if ALTERNATIVES[key] == config.get(defaults(), key):
                continue
            with self.subTest(key=key):
                self.assertNotEqual(config.config_fingerprint(with_value(defaults(), key, ALTERNATIVES[key])), base)

    def test_unknown_stage_or_key(self):
        with self.assertRaises(KeyError):
            stage_fingerprint(defaults(), "s9_nope")
        with self.assertRaises(KeyError):
            config.config_fingerprint(defaults(), ("video.nope",))


if __name__ == "__main__":
    unittest.main()
