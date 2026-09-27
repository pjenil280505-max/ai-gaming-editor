"""C6 overview: every match's latest result and every S3 encode from the run logs."""

from __future__ import annotations

import unittest

from core import overview, run
from fake_drive import FakeDrive

# A C5 log as 0.4 wrote it: no CPU model yet, S3 at 1.31x
LOG_0_4 = """S0 Preflight: checking Colab, Drive and 'clip.mp4' ...
  ffmpeg version 6.1.1-3ubuntu5; 2 CPUs; GPU: none (GPU encoder not available); 80.0 GB free on local disk
S3 Master: encoding 11:41 at 60 fps (the slow step; progress every 5%) ...
  encoding took 15:18 = 1.31x real time (within the D1 limit of 1.5x)
"""
# 0.5.1 on Colab, clip 1's second run: 2.10x on an unrecorded machine type
LOG_0_5 = """S0 Preflight: checking Colab, Drive and 'clip.mp4' ...
  ffmpeg version 6.1.1-3ubuntu5; 2 CPUs (AMD EPYC 7B12); GPU: none (GPU encoder not available); 80.0 GB free on local disk
S3 Master: encoding 11:41 at 60 fps (the slow step; progress every 5%) ...
  encoding took 24:30 = 2.10x real time (OVER the D1 limit of 1.5x)
"""
# A re-run that skipped S3 records no encode
LOG_SKIPPED = """S0 Preflight: checking Colab, Drive and 'clip.mp4' ...
  ffmpeg version 6.1.1-3ubuntu5; 2 CPUs (model unknown); GPU: none (GPU encoder not available); 80.0 GB free on local disk
S3 Master: skipped (done in an earlier run)
"""


class OverviewTest(unittest.TestCase):

    def setUp(self):
        self.drive = FakeDrive()
        self.addCleanup(self.drive.cleanup)

    def text(self):
        return overview.text(self.drive.config_path)

    def test_no_work_folder_or_no_matches(self):
        self.assertIn("not found. Run cell C1", self.text())
        self.drive.work.mkdir(parents=True)
        (self.drive.work / "notes").mkdir()                        # not a match folder
        self.assertIn("No matches in", self.text())

    def test_matches_and_encodes_after_real_runs(self):
        self.drive.add("F1")
        self.drive.add("F7")
        for name in ("F1.mp4", "F7.mp4", "F1.mp4"):                # the F1 re-run skips S3
            result = run.run_pipeline(name, config_path=self.drive.config_path, log=lambda _: None)
        text = self.text()
        matches = sorted(p.name for p in self.drive.work.iterdir())
        for match in matches:
            self.assertIn(match, text)
        self.assertIn(f"  {result.match_id}    0:20  PASS       6 pass", text)    # F1
        self.assertIn("WARN", text)                                              # F7: late audio
        encode_rows = [l for l in text.splitlines() if "x  " in l and l.startswith("  20")]
        self.assertEqual(len(encode_rows), 2)                                    # not 3
        self.assertIn("2 encodes:", text)
        self.assertIn("0 over the D1 limit of 1.5x", text)

    def test_encodes_from_older_logs_and_their_machines(self):
        match = self.drive.work / "20260821-2154-61cab8"
        (match / "logs").mkdir(parents=True)
        (match / "logs" / "run-20260926T141000Z.txt").write_text(LOG_0_4)
        (match / "logs" / "run-20260927T235500Z.txt").write_text(LOG_0_5)
        (match / "logs" / "run-20260928T090000Z.txt").write_text(LOG_SKIPPED)
        (match / "logs" / "copy of run.txt").write_text(LOG_0_5)          # only run-<id>.txt counts
        self.assertEqual([(e.run_id, e.speed, e.cpus, e.cpu_model, e.footage_s)
                          for e in overview.encodes(match)],
                         [("20260926T141000Z", 1.31, 2, None, 701),
                          ("20260927T235500Z", 2.1, 2, "AMD EPYC 7B12", 701)])
        text = self.text()
        self.assertIn("no readable run_report.json", text)
        self.assertIn("2026-09-26 14:10  20260821-2154-61cab8   11:41   1.31x  2 CPUs, CPU not recorded",
                      text)
        self.assertIn("2026-09-27 23:55  20260821-2154-61cab8   11:41   2.10x  2 CPUs, AMD EPYC 7B12",
                      text)
        self.assertIn("2 encodes: 1.31x to 2.10x real time; 1 over the D1 limit of 1.5x", text)


if __name__ == "__main__":
    unittest.main()
