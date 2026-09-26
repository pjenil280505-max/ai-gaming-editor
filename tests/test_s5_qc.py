"""S5 QC v0: each check's pass/warn/fail rule, the FFmpeg measurements, qc.json, failures."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fixtures
from core import ffmpeg, s0_preflight, s1_stage_in, s2_probe, s3_master, s4_analysis, s5_qc
from core.contracts import QcCheck, QcResults
from core.errors import StageError
from fake_drive import FakeDrive

_tmp: tempfile.TemporaryDirectory
_media: dict[str, Path] = {}
_drives: list[FakeDrive] = []
_built: dict = {}

REFERENCE_DBFS = -23        # EBU Tech 3341 case 1: a stereo 1 kHz sine at -23 dBFS reads -23.0 LUFS


def ffmpeg_make(out: Path, *args: str) -> Path:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args, str(out)],
                   check=True, capture_output=True)
    return out


def media(name: str) -> Path:
    """Test media made at test time (CLAUDE.md rule 7)."""
    if name not in _media:
        out = Path(_tmp.name) / f"{name}.mp4"
        video = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
        if name == "spans":
            # 12 s at 10 fps like an analysis copy: 0–3 black, 3–6 moving, 6–9 still, 9–12 black
            parts = ["color=c=black:s=320x180:r=10:d=3", "testsrc2=s=320x180:r=10:d=3",
                     "color=c=red:s=320x180:r=10:d=3", "color=c=black:s=320x180:r=10:d=3"]
            inputs = [a for part in parts for a in ("-f", "lavfi", "-i", part)]
            ffmpeg_make(out, *inputs, "-filter_complex", "[0][1][2][3]concat=n=4:v=1:a=0", *video)
        elif name == "tone":
            amplitude = 10 ** (REFERENCE_DBFS / 20)
            sine = f"{amplitude}*sin(2*PI*1000*t)"
            ffmpeg_make(out, "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=20",
                        "-f", "lavfi", "-i", f"aevalsrc={sine}|{sine}:s=48000:d=20",
                        *video, "-c:a", "aac", "-b:a", "192k", "-shortest")
        elif name == "silence":
            ffmpeg_make(out, "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=5",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "5",
                        *video, "-c:a", "aac")
        _media[name] = out
    return _media[name]


def setUpModule():
    global _tmp
    _tmp = tempfile.TemporaryDirectory(prefix="aieditor-s5-")


def tearDownModule():
    _tmp.cleanup()
    for drive in _drives:
        drive.cleanup()


def measure(command: list[str], duration_s: float) -> str:
    log = Path(_tmp.name) / "measure.log"
    ffmpeg.run_with_progress(command, duration_s, None, log)
    return log.read_text()


def build(fixture: str, **changes):
    """(drive, pre, stage_in, probe, master, analysis) for a fixture; cached per config."""
    key = (fixture, tuple(sorted(changes.items())))
    if key not in _built:
        drive = FakeDrive({k.replace("__", "."): v for k, v in changes.items()})
        _drives.append(drive)
        pre = s0_preflight.run(drive.add(fixture), None, drive.config_path)
        stage_in = s1_stage_in.run(pre)
        probe = s2_probe.run(pre, stage_in)
        master = s3_master.run(pre, stage_in, probe)
        _built[key] = (drive, pre, stage_in, probe, master, s4_analysis.run(pre, stage_in, master))
    return _built[key]


def run_s5(fixture: str, progress=None, **changes):
    drive, pre, stage_in, probe, master, analysis = build(fixture, **changes)
    return s5_qc.run(pre, stage_in, probe.media_info, master.local_path, analysis.local_video,
                     progress=progress)


def by_name(qc) -> dict[str, QcCheck]:
    return {c.check: c for c in qc.checks}


def streams(video_s: float, audio_s: float | None) -> list[dict]:
    out = [{"codec_type": "video", "start_time": "0.000000", "duration": str(video_s)}]
    if audio_s is not None:
        out.append({"codec_type": "audio", "start_time": "0.000000", "duration": str(audio_s)})
    return out


class SpanScanTest(unittest.TestCase):

    def test_black_and_frozen_spans_including_ones_that_run_to_the_end(self):
        black, frozen = s5_qc.parse_spans(measure(s5_qc.scan_command(media("spans")), 12.0), 12.0, 0.1)
        self.assertEqual(black, [{"start_s": 0.0, "end_s": 3.0}, {"start_s": 9.0, "end_s": 12.0}])
        # a black picture is also a still one
        self.assertEqual(frozen, [{"start_s": 0.0, "end_s": 3.0}, {"start_s": 6.0, "end_s": 9.0},
                                  {"start_s": 9.0, "end_s": 12.0}])

    def test_moving_picture_with_flashes_has_no_spans(self):
        self.assertEqual(s5_qc.parse_spans(measure(s5_qc.scan_command(fixtures.path("F1")), 20.0),
                                           20.0, 1 / 30), ([], []))

    def test_parse_spans_from_log_lines(self):
        log = ("[blackdetect @ 0x1] black_start:1.5 black_end:4 black_duration:2.5\n"
               "[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 20\n"
               "[freezedetect @ 0x2] lavfi.freezedetect.freeze_duration: 3\n"
               "[freezedetect @ 0x2] lavfi.freezedetect.freeze_end: 23\n"
               "[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 57.3\n")
        black, frozen = s5_qc.parse_spans(log, 60.0, 0.1)
        self.assertEqual(black, [{"start_s": 1.5, "end_s": 4.0}])
        self.assertEqual(frozen, [{"start_s": 20.0, "end_s": 23.0}, {"start_s": 57.3, "end_s": 60.0}])
        self.assertEqual(s5_qc.parse_spans("", 60.0, 0.1), ([], []))


class LoudnessTest(unittest.TestCase):

    def test_reference_tone_reads_minus_23_lufs(self):
        found = s5_qc.parse_loudness(measure(s5_qc.loudness_command(media("tone")), 20.0))
        self.assertAlmostEqual(found["integrated_lufs"], -23.0, delta=0.2)
        self.assertAlmostEqual(found["true_peak_dbtp"], -23.0, delta=1.0)   # AAC adds a little
        self.assertAlmostEqual(found["lra_lu"], 0.0, delta=0.5)

    def test_silence_has_no_peak_and_stays_valid_json(self):
        found = s5_qc.parse_loudness(measure(s5_qc.loudness_command(media("silence")), 5.0))
        self.assertEqual(found["integrated_lufs"], -70.0)     # ebur128's floor
        self.assertIsNone(found["true_peak_dbtp"])            # -inf dBFS
        check = s5_qc.check_loudness(found)
        self.assertEqual(QcResults(checks=[check]).validate(), [])
        self.assertIn("silent", s5_qc.describe(check))

    def test_missing_summary_is_none(self):
        self.assertIsNone(s5_qc.parse_loudness(""))
        self.assertIsNone(s5_qc.parse_loudness("Summary:\n  I: -20.0 LUFS\n"))


class RulesTest(unittest.TestCase):

    def test_length_vs_recording(self):
        self.assertEqual(s5_qc.check_length(20.1, 20.0, 0.1).status, "pass")
        self.assertEqual(s5_qc.check_length(19.95, 20.0, 0.1).status, "pass")
        late = s5_qc.check_length(20.25, 20.0, 0.1)
        self.assertEqual((late.status, late.value, late.threshold), ("fail", 0.25, 0.1))
        self.assertEqual(s5_qc.describe(late),
                         "master is 0.250 s longer than the recording (limit 0.100 s)")
        self.assertIn("shorter", s5_qc.describe(s5_qc.check_length(19.95, 20.0, 0.1)))
        self.assertIn("same length", s5_qc.describe(s5_qc.check_length(20.0, 20.0, 0.1)))

    def test_audio_vs_video_allows_one_frame_plus_one_audio_block(self):
        # 60 fps, 48 kHz recording: 16.7 ms frame + 21.3 ms AAC block = 38.0 ms (DEC-028)
        check = s5_qc.check_audio_vs_video
        self.assertEqual(check(streams(20.0, 20.021), 60, 1, 48000).status, "pass")   # padding only
        self.assertEqual(check(streams(20.0, 20.038), 60, 1, 48000).status, "pass")
        self.assertEqual(check(streams(20.0, 20.039), 60, 1, 48000).status, "warn")
        self.assertEqual(check(streams(20.0, 19.961), 60, 1, 48000).status, "warn")   # ends early
        self.assertEqual(check(streams(20.0, 20.056), 30, 1, 44100).status, "pass")   # 33.3 + 23.2 ms
        self.assertEqual(check(streams(20.0, 20.021), 60, 0, 48000).status, "pass")   # block only
        result = check(streams(20.0, 20.1), 60, 1, 48000)
        self.assertEqual((result.value, result.threshold), (0.1, 0.038))
        self.assertEqual(s5_qc.describe(result), "sound ends 100 ms after the picture (limit 38 ms)")
        self.assertIn("before", s5_qc.describe(check(streams(20.0, 19.9), 60, 1, 48000)))

    def test_no_sound_warns_instead_of_guessing(self):
        for result in (s5_qc.check_audio_vs_video(streams(20.0, None), 30, 1, None),
                       s5_qc.check_loudness(None)):
            self.assertEqual((result.status, result.value, result.threshold), ("warn", None, None))
            self.assertIn("no sound", s5_qc.describe(result))

    def test_spans_warn_and_are_listed_for_the_owner(self):
        self.assertEqual(s5_qc.check_spans("black_spans", []).status, "pass")
        spans = [{"start_s": 61.0, "end_s": 64.5}, {"start_s": 600.0, "end_s": 602.0}]
        result = s5_qc.check_spans("frozen_spans", spans)
        self.assertEqual((result.status, result.threshold), ("warn", 2.0))
        self.assertEqual(s5_qc.describe(result), "1:01–1:04 (3.5 s), 10:00–10:02 (2.0 s)")

    def test_outcome_lines(self):
        checks = [s5_qc.check_length(20.25, 20.0, 0.1), s5_qc.check_loudness(None),
                  s5_qc.check_spans("black_spans", [])]
        warnings, failures = s5_qc.outcome(checks)
        self.assertEqual(warnings, ["Loudness: no sound, nothing to measure"])
        self.assertEqual(failures, ["Length vs recording: master is 0.250 s longer than the "
                                    "recording (limit 0.100 s). Send this message."])
        self.assertEqual(s5_qc.lines(checks)[0],
                         "  Length vs recording    fail  master is 0.250 s longer than the "
                         "recording (limit 0.100 s)")


class FrameRateCheckTest(unittest.TestCase):

    def test_variable_rate_file_fails(self):
        result = s5_qc.check_frame_rate(fixtures.path("F2"), 60)
        self.assertEqual(result.status, "fail")
        self.assertIn("master frames are not 1/60 s long", result.value)

    def test_master_passes(self):
        master = build("F2")[4]
        result = s5_qc.check_frame_rate(master.local_path, 60)
        self.assertEqual((result.status, result.value), ("pass", "every frame lasts 1/60 s"))


class StageTest(unittest.TestCase):

    def test_every_check_passes_on_f1_and_qc_json_is_on_drive(self):
        drive, pre, stage_in, *_ = build("F1")
        qc = run_s5("F1")
        self.assertEqual([c.check for c in qc.checks], list(s5_qc.LABELS))
        self.assertEqual({c.check: c.status for c in qc.checks},
                         dict.fromkeys(s5_qc.LABELS, "pass"))
        self.assertEqual((qc.warnings, qc.failures), ([], []))
        self.assertEqual(qc.drive_path, drive.work / stage_in.match_id / "qc.json")
        self.assertEqual(QcResults.from_json(qc.drive_path.read_text()).checks, qc.checks)
        self.assertEqual(s5_qc.load(qc.drive_path.parent), qc.checks)
        for log in (s5_qc.SCAN_LOG, s5_qc.LOUDNESS_LOG):
            self.assertTrue((stage_in.local_dir / "logs" / log).is_file())

    def test_measured_values_on_f1(self):
        checks = by_name(run_s5("F1"))
        # F1's recording ends its last AAC block 15 ms after the picture (DEC-022)
        self.assertAlmostEqual(checks["audio_vs_video_length"].value, 0.015, delta=0.002)
        self.assertAlmostEqual(checks["audio_vs_video_length"].threshold, 1 / 30 + 1024 / 44100,
                               places=4)
        self.assertAlmostEqual(checks["length_vs_recording"].value, 0.015, delta=0.002)
        self.assertEqual(checks["length_vs_recording"].threshold, 0.1)
        self.assertEqual(set(checks["loudness"].value), {"integrated_lufs", "true_peak_dbtp", "lra_lu"})

    def test_variable_rate_recording_at_60_fps(self):
        checks = by_name(run_s5("F2"))
        self.assertEqual({c.status for c in checks.values()}, {"pass"})
        self.assertAlmostEqual(checks["audio_vs_video_length"].threshold, 1 / 60 + 1024 / 44100,
                               places=4)

    def test_video_only_warns_on_the_sound_checks(self):
        drive, pre, stage_in, *_ = build("F4")
        qc = run_s5("F4")
        self.assertEqual({c.check: c.status for c in qc.checks if c.status != "pass"},
                         {"audio_vs_video_length": "warn", "loudness": "warn"})
        self.assertEqual(len(qc.warnings), 2)
        self.assertFalse((stage_in.local_dir / "logs" / s5_qc.LOUDNESS_LOG).exists())

    def test_failed_check_is_returned_and_saved_not_raised(self):
        qc = run_s5("F1", qc__duration_tolerance_s=0.001)
        self.assertEqual(by_name(qc)["length_vs_recording"].status, "fail")
        self.assertEqual(len(qc.failures), 1)
        self.assertEqual(s5_qc.load(qc.drive_path.parent), qc.checks)

    def test_tolerance_frames_come_from_config(self):
        checks = by_name(run_s5("F1", qc__av_sync_tolerance_frames=0))
        self.assertAlmostEqual(checks["audio_vs_video_length"].threshold, 1024 / 44100, places=4)

    def test_progress_runs_in_order_to_100_percent(self):
        seen = []
        run_s5("F7", progress=seen.append)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 1.0)
        self.assertIn(s5_qc.SCAN_SHARE, seen)            # the scan's own end

    def test_ffmpeg_failure_is_plain_english(self):
        def broken(*args, **kwargs):
            raise ffmpeg.ToolError("Invalid data found when processing input")

        with mock.patch.object(ffmpeg, "run_with_progress", broken):
            with self.assertRaises(StageError) as ctx:
                run_s5("F1")
        self.assertTrue(str(ctx.exception).startswith("S5 QC failed:"))
        self.assertIn("FFmpeg could not check the master (Invalid data", str(ctx.exception))

    def test_load_missing_or_unreadable_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(s5_qc.load(Path(tmp)))
            (Path(tmp) / "qc.json").write_text('{"checks": [{"check": ""}]}')
            self.assertIsNone(s5_qc.load(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
