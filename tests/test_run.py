"""core/run.py: inbox listing, choosing a recording, S0 → S2 end to end."""

from __future__ import annotations

import os
import time
import unittest

from core import run
from core.errors import StageError
from fake_drive import FakeDrive


class InboxTest(unittest.TestCase):

    def setUp(self):
        self.drive = FakeDrive()
        self.addCleanup(self.drive.cleanup)

    def add_at(self, fixture, name, age_s):
        self.drive.add(fixture, name)
        t = time.time() - age_s
        os.utime(self.drive.inbox / name, (t, t))

    def test_listing_is_numbered_newest_first(self):
        self.add_at("F1", "old.mp4", 100)
        self.add_at("F1", "new.mp4", 10)
        (self.drive.inbox / ".hidden").write_text("x")
        (self.drive.inbox / "folder").mkdir()
        listing = run.inbox_listing(self.drive.config_path)
        self.assertIn("1. new.mp4", listing)
        self.assertIn("2. old.mp4", listing)
        self.assertNotIn(".hidden", listing)
        self.assertNotIn("folder", listing)
        self.assertEqual(run.resolve_choice("1", self.drive.config_path), "new.mp4")
        self.assertEqual(run.resolve_choice(" 2 ", self.drive.config_path), "old.mp4")
        self.assertEqual(run.resolve_choice("3", self.drive.config_path), "3")
        self.assertEqual(run.resolve_choice("old.mp4", self.drive.config_path), "old.mp4")

    def test_listing_explains_empty_or_missing_inbox(self):
        self.assertIn("is empty", run.inbox_listing(self.drive.config_path))
        self.drive.inbox.rmdir()
        self.assertIn("not found", run.inbox_listing(self.drive.config_path))

    def test_overrides(self):
        self.assertEqual(run.overrides_for("config"), {})
        self.assertEqual(run.overrides_for("auto"), {"video.target_fps": "auto"})
        self.assertEqual(run.overrides_for("60"), {"video.target_fps": 60})
        with self.assertRaises(ValueError):
            run.overrides_for("25")


class RunToProbeTest(unittest.TestCase):

    def test_end_to_end_with_log(self):
        with FakeDrive() as drive:
            drive.add("F5")
            lines = []
            result = run.run_to_probe("1", target_fps="60", config_path=drive.config_path,
                                      log=lines.append)
            self.assertTrue(result.drive_media_info.is_file())
            self.assertEqual(result.media_info.target_fps, 60)
            self.assertEqual(result.drive_media_info.parent.name, result.match_id)
            text = "\n".join(lines)
            for heading in ("S0 Preflight", "S1 Stage-in", "100%", "(time from file modified time",
                            "S2 Probe", "WARNING: Found 2 audio tracks"):
                self.assertIn(heading, text)
            self.assertEqual(result.warnings, result.media_info.warnings)

    def test_failure_raises_stage_error(self):
        with FakeDrive() as drive:
            drive.add("F9")
            with self.assertRaises(StageError) as ctx:
                run.run_to_probe("F9.mp4", config_path=drive.config_path, log=lambda _: None)
            self.assertTrue(str(ctx.exception).startswith("S2 Probe failed:"))


if __name__ == "__main__":
    unittest.main()
