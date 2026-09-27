"""core/ffmpeg.py: run_with_progress reports the whole run, and nothing past a failure."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import ffmpeg

# Video only, so FFmpeg's own last out_time is the last frame's start: 1.96 s of 2 s.
VIDEO_ONLY = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
              "-f", "lavfi", "-i", "testsrc2=s=64x36:r=25:d=2", "-f", "null", "-"]


class RunWithProgressTest(unittest.TestCase):

    def test_success_ends_at_exactly_1(self):
        # S4 stopped at 90% on Colab: FFmpeg's last report fell just short of the end (DEC-029)
        seen = []
        ffmpeg.run_with_progress(VIDEO_ONLY, 2.0, seen.append)
        self.assertEqual(seen, sorted(seen))
        self.assertLess(max(seen[:-1]), 1.0)
        self.assertEqual(seen[-1], 1.0)

    def test_failure_never_reports_the_end(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "x.log"
            with self.assertRaises(ffmpeg.ToolError):
                ffmpeg.run_with_progress(VIDEO_ONLY[:-3] + ["-c:v", "no_such_codec", "-f", "null", "-"],
                                         2.0, seen.append, log)
            self.assertIn("no_such_codec", log.read_text())
        self.assertNotIn(1.0, seen)

    def test_no_progress_callback_is_fine(self):
        ffmpeg.run_with_progress(VIDEO_ONLY, 2.0)


if __name__ == "__main__":
    unittest.main()
