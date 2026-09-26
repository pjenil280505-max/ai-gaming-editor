"""S0 Preflight: environment and input checks, all problems reported together."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import ffmpeg, s0_preflight
from core.errors import StageError
from fake_drive import FakeDrive


class PreflightTest(unittest.TestCase):

    def setUp(self):
        self.drive = FakeDrive()
        self.addCleanup(self.drive.cleanup)

    def run_s0(self, name, overrides=None):
        return s0_preflight.run(name, overrides, self.drive.config_path)

    def problems(self, name, overrides=None):
        with self.assertRaises(StageError) as ctx:
            self.run_s0(name, overrides)
        self.assertTrue(str(ctx.exception).startswith("S0 Preflight failed:"))
        return ctx.exception.problems

    def test_happy_path_records_environment(self):
        name = self.drive.add("F1")
        pre = self.run_s0(name)
        self.assertEqual(pre.source, self.drive.inbox / name)
        self.assertEqual(pre.source_size_bytes, (self.drive.inbox / name).stat().st_size)
        self.assertTrue(pre.ffmpeg_version.startswith("ffmpeg version 6."))
        self.assertTrue(pre.ffprobe_version.startswith("ffprobe version 6."))
        self.assertGreaterEqual(pre.cpu_count, 1)
        self.assertGreater(pre.free_disk_bytes, 0)
        self.assertEqual(pre.paths.local_work, self.drive.local_work)
        self.assertTrue(pre.platform)
        if not ffmpeg.available("nvidia-smi"):
            self.assertIsNone(pre.gpu)
            self.assertFalse(pre.gpu_encoder)

    def test_override_is_applied_and_validated(self):
        name = self.drive.add("F1")
        pre = self.run_s0(name, {"video.target_fps": 60})
        self.assertEqual(pre.config["video"]["target_fps"], 60)
        problems = self.problems(name, {"video.target_fps": 25})
        self.assertEqual(problems, ["Config: video.target_fps: expected auto, 30 or 60, got 25"])

    def test_drive_not_mounted(self):
        self.drive.add("F1")
        for child in sorted(self.drive.drive_root.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.drive.drive_root.rmdir()
        problems = self.problems("F1.mp4")
        self.assertEqual(len(problems), 1)
        self.assertIn("Run cell C1", problems[0])

    def test_inbox_missing(self):
        self.drive.inbox.rmdir()
        problems = self.problems("F1.mp4")
        self.assertIn("Inbox folder", problems[0])

    def test_recording_missing_or_not_a_plain_name(self):
        self.assertIn("not found", self.problems("nope.mp4")[0])
        self.assertIn("not a plain file name", self.problems("../pipeline.yaml")[0])
        self.assertIn("not a plain file name", self.problems("")[0])

    def test_not_enough_disk(self):
        name = self.drive.add("F1")
        with mock.patch.object(s0_preflight, "free_bytes", return_value=1000):
            problems = self.problems(name)
        self.assertEqual(len(problems), 1)
        self.assertIn("Not enough free space", problems[0])
        self.assertIn("3 x", problems[0])

    def test_missing_ffmpeg_and_bad_config_reported_together(self):
        with tempfile.TemporaryDirectory() as empty:
            config = self.drive.config_path.read_text().replace("master_crf: 18", "master_crf: 99")
            self.drive.config_path.write_text(config)
            with mock.patch.dict(os.environ, {"PATH": empty}):
                problems = self.problems("F1.mp4")
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(problems[0].startswith("Config: video.master_crf"))
        self.assertTrue(problems[1].startswith("ffmpeg is not installed"))
        self.assertTrue(problems[2].startswith("ffprobe is not installed"))

    def test_gpu_checks_without_gpu(self):
        with tempfile.TemporaryDirectory() as empty, mock.patch.dict(os.environ, {"PATH": empty}):
            self.assertIsNone(s0_preflight.gpu_name())
            self.assertFalse(s0_preflight.gpu_encoder_works())

    def test_free_bytes_uses_nearest_existing_parent(self):
        missing = self.drive.local_work / "not" / "yet"
        self.assertGreater(s0_preflight.free_bytes(missing), 0)

    def test_human_size(self):
        self.assertEqual(s0_preflight.human_size(999), "999 bytes")
        self.assertEqual(s0_preflight.human_size(1_500_000), "1.5 MB")
        self.assertEqual(s0_preflight.human_size(3_200_000_000), "3.2 GB")
        self.assertEqual(s0_preflight.human_size(12_000_000_000_000), "12000.0 GB")


if __name__ == "__main__":
    unittest.main()
