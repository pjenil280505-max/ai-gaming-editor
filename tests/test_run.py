"""core/run.py: inbox listing, choosing a recording, progress lines, S0 → S4 end to end."""

from __future__ import annotations

import os
import time
import unittest

from core import VERSION, run
from core.contracts import RunReport
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

    def test_describe_choice(self):
        self.add_at("F1", "clip.mp4", 10)
        self.assertIn("Selected: clip.mp4", run.describe_choice("1", self.drive.config_path))
        self.assertIn("Now run cell C5", run.describe_choice("clip.mp4", self.drive.config_path))
        for bad in ("2", "nope.mp4", "../pipeline.yaml"):
            with self.subTest(choice=bad):
                answer = run.describe_choice(bad, self.drive.config_path)
                self.assertIn("is not a recording in the inbox", answer)
                self.assertIn("1. clip.mp4", answer)


class ProgressLinesTest(unittest.TestCase):

    def test_steps_log_each_step_once(self):
        lines = []
        steps = run.Steps(lines.append, 25)
        for fraction in (0.1, 0.26, 0.3, 0.74, 0.75, 1.0, 1.0):
            steps(fraction)
        self.assertEqual(lines, ["  25%", "  50%", "  75%", "  100%"])

    def test_media_steps_show_position_speed_and_time_left(self):
        lines = []
        steps = run.media_steps(lines.append, 600.0, 5)
        time.sleep(0.05)
        steps(0.5)
        self.assertRegex(lines[0], r"^  50% · 5:00 of 10:00 · [0-9.]+x real time · about [0-9]+:[0-9]{2} left$")

    def test_clock(self):
        self.assertEqual(run.clock(0), "0:00")
        self.assertEqual(run.clock(700.8), "11:41")
        self.assertEqual(run.clock(3725), "62:05")


class RunPipelineTest(unittest.TestCase):

    def test_end_to_end_with_log(self):
        with FakeDrive() as drive:
            drive.add("F5")
            lines = []
            result = run.run_pipeline("1", target_fps="60", config_path=drive.config_path,
                                      log=lines.append)
            self.assertEqual(result.drive_dir, drive.work / result.match_id)
            self.assertEqual(sorted(p.name for p in result.drive_dir.iterdir()),
                             ["analysis.mp4", "analysis.wav", "logs", "master.mp4", "media_info.json",
                              "run_report.json", "stages", "summary.txt"])
            self.assertEqual(result.skipped, [])
            self.assertEqual(result.media_info.target_fps, 60)
            self.assertEqual(result.master.fps, 60)
            text = "\n".join(lines)
            for heading in ("S0 Preflight", "S1 Stage-in", "(time from file modified time",
                            "S2 Probe", "WARNING: Found 2 audio tracks", "S3 Master", "% · ",
                            "x real time (within the D1 limit of 1.5x)", "copied to Drive 100%",
                            "S4 Analysis copy", "analysis.mp4 640x360, 10 fps", "S6 Stage-out",
                            "Done. Match", "Watch master.mp4 (full quality)"):
                self.assertIn(heading, text)
            self.assertEqual(result.warnings, result.media_info.warnings)

    def test_video_only_warns_at_s3_and_s4(self):
        with FakeDrive() as drive:
            drive.add("F4")
            lines = []
            result = run.run_pipeline("1", config_path=drive.config_path, log=lines.append)
            self.assertNotIn("analysis.wav", [p.name for p in result.drive_dir.iterdir()])
            self.assertEqual(len(result.warnings), 3)
            self.assertIn("WARNING: The master has no sound", "\n".join(lines))

    def test_failure_raises_stage_error_after_writing_the_report(self):
        with FakeDrive() as drive:
            drive.add("F9")
            with self.assertRaises(StageError) as ctx:
                run.run_pipeline("F9.mp4", config_path=drive.config_path, log=lambda _: None)
            self.assertTrue(str(ctx.exception).startswith("S2 Probe failed:"))
            (match_dir,) = drive.work.iterdir()
            report = RunReport.from_json((match_dir / "run_report.json").read_text())
            self.assertEqual([(s.name, s.status) for s in report.stages],
                             [("s0_preflight", "pass"), ("s1_stage_in", "pass"),
                              ("s2_probe", "fail"), ("s6_stage_out", "pass")])
            self.assertIn("Could not read F9.mp4", report.stages[2].errors[0])
            self.assertIn("Result: FAILED at S2 Probe", (match_dir / "summary.txt").read_text())
            self.assertFalse((match_dir / "media_info.json").exists())
            self.assertFalse((match_dir / "stages").exists())

    def test_notebook_from_another_release_is_refused(self):
        for version in (None, "0.3.0"):
            with self.subTest(version=version):
                with self.assertRaises(StageError) as ctx:
                    run.run_pipeline("1", notebook_version=version, log=lambda _: None)
                message = str(ctx.exception)
                self.assertTrue(message.startswith("Notebook check failed:"))
                self.assertIn(f"blob/v{VERSION}/notebooks/run_pipeline.ipynb", message)


