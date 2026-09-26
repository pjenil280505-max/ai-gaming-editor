"""S4 Analysis copy: size, rate, WAV format, timing kept, durations, video-only, failures."""

from __future__ import annotations

import unittest
from unittest import mock

import measure
from core import s0_preflight, s1_stage_in, s2_probe, s3_master, s4_analysis
from core.errors import StageError
from fake_drive import FakeDrive

_drives: list[FakeDrive] = []
_built: dict = {}


def build(fixture: str, **changes):
    """(drive, pre, stage_in, master, analysis) for a fixture; cached per config."""
    key = (fixture, tuple(sorted(changes.items())))
    if key not in _built:
        drive = FakeDrive({k.replace("__", "."): v for k, v in changes.items()})
        _drives.append(drive)
        name = drive.add(fixture)
        pre = s0_preflight.run(name, None, drive.config_path)
        stage_in = s1_stage_in.run(pre)
        master = s3_master.run(pre, stage_in, s2_probe.run(pre, stage_in))
        _built[key] = (drive, pre, stage_in, master, s4_analysis.run(pre, stage_in, master))
    return _built[key]


def tearDownModule():
    for drive in _drives:
        drive.cleanup()


def duration(path) -> float:
    return float(measure.probe(path)["format"]["duration"])


class AnalysisVideoTest(unittest.TestCase):

    def test_width_640_aspect_kept_even_height(self):
        for fixture, size in (("F2", (640, 360)), ("F8", (640, 288)), ("F3", (640, 1138))):
            with self.subTest(fixture=fixture):
                drive, pre, stage_in, master, analysis = build(fixture)
                video = measure.streams(measure.probe(analysis.local_video), "video")[0]
                self.assertEqual((video["width"], video["height"]), size)
                self.assertEqual((analysis.width, analysis.height), size)
                self.assertEqual(video["height"] % 2, 0)
                self.assertAlmostEqual(size[0] / size[1], master.width / master.height, delta=0.01 * size[0] / size[1])

    def test_10_fps_constant_no_audio_and_duration_matches_master(self):
        drive, pre, stage_in, master, analysis = build("F2")
        info = measure.probe(analysis.local_video)
        self.assertEqual([s["codec_type"] for s in info["streams"]], ["video"])
        durations = measure.video_frame_durations(analysis.local_video)
        self.assertTrue(all(abs(d - 0.1) < 1e-6 for d in durations))
        self.assertLessEqual(abs(duration(analysis.local_video) - master.duration_s), 0.1)

    def test_picture_stays_within_one_analysis_frame(self):
        # A one-frame flash can fall between 10 fps samples, so compare pictures instead, at
        # 2 s (no flash nearby): the best-matching master frame must be within 0.1 s.
        drive, pre, stage_in, master, analysis = build("F7")
        scores = {}
        for k in range(-6, 7):
            t = round(2.0 + k / 30, 4)
            scores[t] = measure.psnr(["-ss", "2", "-i", str(analysis.local_video)],
                                     ["-ss", str(t), "-i", str(master.local_path)],
                                     "[1:v]scale=640:360[m];[0:v][m]psnr")
        best = max(scores, key=scores.get)
        self.assertLessEqual(abs(best - 2.0), 0.1, scores)


class AnalysisAudioTest(unittest.TestCase):

    def test_wav_is_mono_16k_pcm_and_matches_master_length(self):
        drive, pre, stage_in, master, analysis = build("F2")
        info = measure.probe(analysis.local_audio)
        audio = info["streams"][0]
        self.assertEqual((audio["codec_name"], int(audio["sample_rate"]), audio["channels"]),
                         ("pcm_s16le", 16000, 1))
        self.assertLessEqual(abs(duration(analysis.local_audio) - master.duration_s), 0.1)

    def test_beeps_stay_in_place(self):
        analysis = build("F7")[4]
        wav = measure.AudioTrack(analysis.local_audio, 0)
        for t in (5.0, 10.0, 15.0):
            self.assertAlmostEqual(wav.onset(t), t, delta=1 / 30)

    def test_settings_come_from_config(self):
        drive, pre, stage_in, master, analysis = build(
            "F1", analysis__width=320, analysis__fps=5, analysis__audio_rate=8000,
            analysis__audio_channels=2)
        video = measure.streams(measure.probe(analysis.local_video), "video")[0]
        self.assertEqual((video["width"], video["height"], video["r_frame_rate"]), (320, 180, "5/1"))
        audio = measure.probe(analysis.local_audio)["streams"][0]
        self.assertEqual((int(audio["sample_rate"]), audio["channels"]), (8000, 2))

    def test_video_only_master_gives_no_wav_and_a_warning(self):
        drive, pre, stage_in, master, analysis = build("F4")
        self.assertIsNone(analysis.local_audio)
        self.assertIsNone(analysis.drive_audio)
        self.assertEqual(analysis.warnings, ["No analysis.wav: the master has no sound."])
        self.assertFalse((stage_in.local_dir / "analysis.wav").exists())


class AnalysisOutputTest(unittest.TestCase):

    def test_saved_locally_and_on_drive(self):
        drive, pre, stage_in, master, analysis = build("F7")
        drive_dir = drive.work / stage_in.match_id
        self.assertEqual(analysis.drive_video, drive_dir / "analysis.mp4")
        self.assertEqual(analysis.drive_audio, drive_dir / "analysis.wav")
        self.assertEqual(analysis.drive_video.read_bytes(), analysis.local_video.read_bytes())
        self.assertEqual(analysis.drive_audio.read_bytes(), analysis.local_audio.read_bytes())
        self.assertFalse(any(p.name.endswith(".partial") for p in drive_dir.iterdir()))

    def test_too_short_output_fails_and_leaves_nothing(self):
        drive = FakeDrive()
        _drives.append(drive)
        name = drive.add("F1")
        pre = s0_preflight.run(name, None, drive.config_path)
        stage_in = s1_stage_in.run(pre)
        master = s3_master.run(pre, stage_in, s2_probe.run(pre, stage_in))
        real = s4_analysis.command

        def truncated(*args, **kwargs):
            cmd = real(*args, **kwargs)
            return cmd[:cmd.index("-i")] + ["-t", "5"] + cmd[cmd.index("-i"):]

        with mock.patch.object(s4_analysis, "command", truncated):
            with self.assertRaises(StageError) as ctx:
                s4_analysis.run(pre, stage_in, master)
        self.assertTrue(str(ctx.exception).startswith("S4 Analysis copy failed:"))
        self.assertIn("longer or shorter than the master", str(ctx.exception))
        self.assertFalse(any(p.name.startswith("analysis") for p in stage_in.local_dir.iterdir()))
        self.assertFalse((drive.work / stage_in.match_id / "analysis.mp4").exists())


if __name__ == "__main__":
    unittest.main()
