"""S3 Master: PHASE0 U3, U4, U5, U6, U7, original audio levels, Drive copy, failures."""

from __future__ import annotations

import math
import shutil
import unittest
from unittest import mock

import fixtures
import measure
from core import ffmpeg, s0_preflight, s1_stage_in, s2_probe, s3_master
from core.errors import StageError
from fake_drive import FakeDrive

_drives: list[FakeDrive] = []
_built: dict = {}


def build(fixture: str, **changes):
    """(drive, pre, stage_in, probe, master) for a fixture; cached per config so each encodes once."""
    key = (fixture, tuple(sorted(changes.items())))
    if key not in _built:
        drive = FakeDrive({k.replace("__", "."): v for k, v in changes.items()})
        _drives.append(drive)
        name = drive.add(fixture)
        pre = s0_preflight.run(name, None, drive.config_path)
        stage_in = s1_stage_in.run(pre)
        probe = s2_probe.run(pre, stage_in)
        _built[key] = (drive, pre, stage_in, probe, s3_master.run(pre, stage_in, probe))
    return _built[key]


def stages_before_s3(fixture: str):
    drive = FakeDrive()
    _drives.append(drive)
    name = drive.add(fixture)
    pre = s0_preflight.run(name, None, drive.config_path)
    stage_in = s1_stage_in.run(pre)
    return drive, pre, stage_in, s2_probe.run(pre, stage_in)


def flash_starts(times: list[float], frame: float) -> list[float]:
    """First frame of each flash; a repeated flash frame (CFR duplication) is one flash."""
    starts = []
    for t in times:
        if not starts or t - last > 1.5 * frame:
            starts.append(t)
        last = t
    return starts


def tearDownModule():
    for drive in _drives:
        drive.cleanup()


class MasterTimingTest(unittest.TestCase):

    def test_U3_variable_rate_master_has_constant_frame_durations(self):
        for fps in (60, 30):
            with self.subTest(fps=fps):
                master = build("F2", video__target_fps=fps)[4]
                durations = measure.video_frame_durations(master.local_path)
                self.assertEqual(len(durations) + 1, 20 * fps)
                self.assertTrue(all(abs(d - 1 / fps) < 1e-6 for d in durations))
                self.assertEqual(s3_master.check_cfr(master.local_path, fps), [])

    def test_U4_flash_and_beep_within_one_frame(self):
        for fixture in ("F2", "F7"):
            master = build(fixture)[4]
            frame = 1 / master.fps
            flashes = flash_starts(measure.flash_times(master.local_path), frame)
            audio = measure.AudioTrack(master.local_path, 0)
            self.assertEqual(audio.start_s, 0.0)
            self.assertEqual(len(flashes), 4, flashes)
            for k, flash in enumerate(flashes):
                with self.subTest(fixture=fixture, t=5 * k):
                    self.assertAlmostEqual(flash, 5 * k, delta=frame)
                    if fixture == "F7" and k == 0:
                        self.assertIsNone(audio.onset(0.0))      # audio only starts at 0.5 s
                        continue
                    onset = audio.onset(5.0 * k)
                    self.assertIsNotNone(onset)
                    self.assertAlmostEqual(onset, 5 * k, delta=frame)
                    self.assertLessEqual(abs(onset - flash), frame)

    def test_late_audio_is_padded_to_start_at_zero(self):
        audio = measure.AudioTrack(build("F7")[4].local_path, 0)
        self.assertEqual(audio.start_s, 0.0)
        self.assertLess(audio.max_level(0.0, 0.45), 0.01 * audio.peak)


class MasterPictureTest(unittest.TestCase):

    def test_U5_rotation_applied_to_pixels_and_cleared(self):
        drive, pre, stage_in, probe, master = build("F3")
        info = measure.probe(master.local_path)
        video = measure.streams(info, "video")[0]
        self.assertEqual((video["width"], video["height"]), (180, 320))
        self.assertEqual(measure.rotation_cw(video), 0)
        self.assertNotIn("side_data_list", video)
        # upright = the source's stored pixels turned 90° clockwise (the rotate=90 meaning)
        a = ["-ss", "2.5", "-i", str(master.local_path)]
        b = ["-noautorotate", "-ss", "2.5", "-i", str(stage_in.local_source)]
        clockwise = measure.psnr(a, b, "[1:v]transpose=clock[r];[0:v][r]psnr")
        counter = measure.psnr(a, b, "[1:v]transpose=cclock[r];[0:v][r]psnr")
        self.assertGreater(clockwise, 20)
        self.assertGreater(clockwise - counter, 10)

    def test_codec_size_and_faststart(self):
        for fixture, size in (("F1", (320, 180)), ("F6", (320, 180)), ("F8", (800, 360))):
            with self.subTest(fixture=fixture):
                master = build(fixture)[4]
                video = measure.streams(measure.probe(master.local_path), "video")[0]
                self.assertEqual(video["codec_name"], "h264")
                self.assertEqual(video["pix_fmt"], "yuv420p")
                self.assertEqual((master.width, master.height), size)
                boxes = measure.top_level_boxes(master.local_path)
                self.assertLess(boxes.index("moov"), boxes.index("mdat"))


class MasterAudioTest(unittest.TestCase):

    def test_audio_is_48k_aac_at_original_level(self):
        drive, pre, stage_in, probe, master = build("F1")
        audio = measure.streams(measure.probe(master.local_path), "audio")[0]
        self.assertEqual((audio["codec_name"], int(audio["sample_rate"]), audio["channels"]),
                         ("aac", 48000, 2))
        before = measure.AudioTrack(stage_in.local_source, 0)
        after = measure.AudioTrack(master.local_path, 0)
        for t in (5.0, 10.0):
            gain_db = 20 * math.log10(after.rms(t + 0.02, t + 0.08) / before.rms(t + 0.02, t + 0.08))
            self.assertLess(abs(gain_db), 0.5, f"level changed by {gain_db:.2f} dB at {t} s")

    def test_U6_no_audio_gives_video_only_master_with_warning(self):
        master = build("F4")[4]
        self.assertFalse(master.has_audio)
        self.assertEqual(measure.streams(measure.probe(master.local_path), "audio"), [])
        self.assertEqual(master.warnings, ["The master has no sound (the recording has no audio track)."])

    def test_U7_master_uses_the_configured_track(self):
        default = measure.AudioTrack(build("F5")[4].local_path, 0)
        self.assertAlmostEqual(default.frequency(5.01, 5.09), 1000, delta=20)
        self.assertIsNone(default.onset(2.5))

        second = build("F5", audio__track_index=1)[4]
        audio = measure.streams(measure.probe(second.local_path), "audio")
        self.assertEqual([(int(a["sample_rate"]), a["channels"]) for a in audio], [(48000, 1)])
        track = measure.AudioTrack(second.local_path, 0)
        for t in (2.5, 7.5, 12.5):
            self.assertAlmostEqual(track.onset(t), t, delta=1 / second.fps)
            self.assertAlmostEqual(track.frequency(t + 0.01, t + 0.09), 440, delta=10)
        self.assertIsNone(track.onset(5.0))


class MasterOutputTest(unittest.TestCase):

    def test_saved_locally_and_on_drive(self):
        drive, pre, stage_in, probe, master = build("F7")
        self.assertEqual(master.local_path, stage_in.local_dir / "master.mp4")
        self.assertEqual(master.drive_path, drive.work / stage_in.match_id / "master.mp4")
        self.assertEqual(master.drive_path.read_bytes(), master.local_path.read_bytes())
        self.assertFalse(any(p.name.endswith(".partial") for p in stage_in.local_dir.iterdir()))
        self.assertGreater(master.encode_seconds, 0)
        self.assertAlmostEqual(master.duration_s, 20.0, delta=0.05)

    def test_progress_reaches_the_end(self):
        drive, pre, stage_in, probe = stages_before_s3("F1")
        seen = []
        s3_master.run(pre, stage_in, probe, progress=seen.append)
        self.assertTrue(seen)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 1.0)                 # DEC-029


class MasterFailureTest(unittest.TestCase):

    def test_failed_encode_never_reports_the_end(self):
        drive, pre, stage_in, probe = stages_before_s3("F1")
        stage_in.local_source.write_bytes(b"not a video")
        seen = []
        with self.assertRaises(StageError):
            s3_master.run(pre, stage_in, probe, progress=seen.append)
        self.assertNotIn(1.0, seen)

    def test_ffmpeg_failure_is_plain_english_and_leaves_nothing(self):
        drive, pre, stage_in, probe = stages_before_s3("F1")
        stage_in.local_source.write_bytes(b"not a video")
        with self.assertRaises(StageError) as ctx:
            s3_master.run(pre, stage_in, probe)
        self.assertTrue(str(ctx.exception).startswith("S3 Master failed:"))
        self.assertIn("FFmpeg could not make the master", str(ctx.exception))
        self.assertFalse(any(p.name.startswith("master.mp4") for p in stage_in.local_dir.iterdir()))
        self.assertFalse((drive.work / stage_in.match_id / "master.mp4").exists())

    def test_frame_check_rejects_uneven_master(self):
        drive, pre, stage_in, probe = stages_before_s3("F1")
        vfr = fixtures.path("F2")

        def fake_encode(cmd, duration_s, progress=None, log_path=None):
            shutil.copyfile(vfr, cmd[-1])         # a variable-rate "master"

        with mock.patch.object(ffmpeg, "run_with_progress", fake_encode):
            with self.assertRaises(StageError) as ctx:
                s3_master.run(pre, stage_in, probe)
        self.assertIn("failed its frame check", str(ctx.exception))
        self.assertFalse(any(p.name.startswith("master.mp4") for p in stage_in.local_dir.iterdir()))

    def test_check_cfr_on_sources(self):
        self.assertEqual(s3_master.check_cfr(fixtures.path("F1"), 30), [])
        self.assertTrue(s3_master.check_cfr(fixtures.path("F1"), 60))
        self.assertTrue(s3_master.check_cfr(fixtures.path("F2"), 60))
        self.assertIn("rotation metadata", s3_master.check_cfr(fixtures.path("F3"), 30)[0])

    def test_check_cfr_ignores_other_side_data(self):
        real = ffmpeg.probe

        def with_hdr_info(path):
            info = real(path)
            for stream in info["streams"]:
                if stream["codec_type"] == "video":
                    stream["side_data_list"] = [{"side_data_type": "Content light level metadata"}]
            return info

        with mock.patch.object(ffmpeg, "probe", with_hdr_info):
            self.assertEqual(s3_master.check_cfr(fixtures.path("F1"), 30), [])


if __name__ == "__main__":
    unittest.main()
