"""S2 Probe: media_info.json for every fixture (PHASE0 U1, U2, U6, U7, U8, U10)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import fixtures
from core import config as cfg
from core import s0_preflight, s1_stage_in, s2_probe
from core.contracts import MediaInfo
from core.errors import StageError
from fake_drive import FakeDrive
from fixtures import SPECS

ASPECTS = {"F3": "9:16", "F8": "20:9"}       # everything else is 320x180 -> 16:9


def config(**changes) -> dict:
    c = cfg.load_config()
    for key, value in changes.items():
        section, name = key.split("__")
        c[section][name] = value
    return c


def measure(name: str, **changes) -> MediaInfo:
    return s2_probe.measure(fixtures.path(name), config(**changes), name)


def run_stages(drive: FakeDrive, name: str):
    pre = s0_preflight.run(name, None, drive.config_path)
    stage_in = s1_stage_in.run(pre)
    return pre, stage_in


class ProbeFixturesTest(unittest.TestCase):

    def test_U1_dimensions_rate_and_tracks_for_every_fixture(self):
        for name in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"):
            spec = SPECS[name]
            with self.subTest(fixture=name):
                info = measure(name)
                v = info.video
                self.assertEqual(v.codec, spec.vcodec)
                self.assertEqual((v.width, v.height), (spec.width, spec.height))
                self.assertEqual(v.rotation, spec.rotation_cw)
                self.assertEqual(v.display_aspect, ASPECTS.get(name, "16:9"))
                self.assertAlmostEqual(info.duration_s, spec.duration_s, delta=0.1)
                self.assertEqual(info.size_bytes, fixtures.path(name).stat().st_size)
                self.assertIn("mp4", info.container)
                self.assertEqual([(t.index, t.codec, t.sample_rate, t.channels) for t in info.audio_tracks],
                                 [(i, "aac", t.sample_rate, t.channels) for i, t in enumerate(spec.tracks)])
                if len(spec.sections) == 1:
                    fps = spec.sections[0].fps
                    self.assertEqual(v.r_frame_rate, f"{fps}/1")
                    self.assertAlmostEqual(v.frame_durations_s.median, 1 / fps, places=6)
                    self.assertAlmostEqual(v.frame_durations_s.stdev, 0.0, places=6)

    def test_U2_constant_vs_variable(self):
        f1, f2 = measure("F1").video, measure("F2").video
        self.assertFalse(f1.is_vfr)
        self.assertIn("0 of 599 frame durations", f1.vfr_evidence)
        self.assertTrue(f2.is_vfr)
        self.assertIn("of 899 frame durations", f2.vfr_evidence)
        self.assertAlmostEqual(f2.frame_durations_s.min, 1 / 60, places=6)
        self.assertAlmostEqual(f2.frame_durations_s.max, 1 / 30, places=6)

    def test_target_fps_auto_and_fixed(self):
        self.assertEqual(measure("F1").target_fps, 30)
        self.assertEqual(measure("F2").target_fps, 60)       # median 45 fps snaps up
        f2_at_30 = measure("F2", video__target_fps=30)
        self.assertEqual(f2_at_30.target_fps, 30)
        self.assertTrue(any("dropped" in w for w in f2_at_30.warnings))
        f1_at_60 = measure("F1", video__target_fps=60)
        self.assertTrue(any("duplicated" in w for w in f1_at_60.warnings))
        self.assertFalse(any("fps" in w for w in measure("F1").warnings))

    def test_rotation_and_aspect(self):
        f3 = measure("F3").video
        self.assertEqual((f3.width, f3.height, f3.rotation, f3.display_aspect), (320, 180, 90, "9:16"))

    def test_U6_no_audio_is_video_only_with_warning(self):
        info = measure("F4")
        self.assertEqual(info.audio_tracks, [])
        self.assertIsNone(info.audio_track_index)
        self.assertTrue(any("video-only" in w for w in info.warnings))

    def test_U7_two_tracks_use_configured_track_with_warning(self):
        info = measure("F5")
        self.assertEqual(info.audio_track_index, 0)
        self.assertTrue(any("Found 2 audio tracks" in w and "using track 0" in w for w in info.warnings))
        self.assertEqual(measure("F5", audio__track_index=1).audio_track_index, 1)
        with self.assertRaises(StageError) as ctx:
            measure("F5", audio__track_index=2)
        self.assertIn("only 2 audio track(s)", str(ctx.exception))

    def test_single_track_no_warning_and_missing_track_fails(self):
        self.assertEqual(measure("F1").warnings, [])
        with self.assertRaises(StageError):
            measure("F1", audio__track_index=1)

    def test_late_audio_offset_reported(self):
        info = measure("F7")
        offset = info.audio_tracks[0].start_offset_s
        self.assertGreaterEqual(offset, 0.5 - 2048 / 44100)   # AAC priming (see test_fixtures F7)
        self.assertLessEqual(offset, 0.5)
        self.assertTrue(any(w.startswith("Audio starts 0.4") for w in info.warnings))

    def test_end_offset_is_where_the_sound_ends_against_the_picture(self):
        # DEC-031: clip 1's phone stopped its sound 60 ms before the picture
        self.assertEqual(measure("F1").audio_tracks[0].end_offset_s, 0.0)
        self.assertAlmostEqual(measure("F7").audio_tracks[0].end_offset_s, 0.0, delta=0.002)
        with tempfile.TemporaryDirectory() as tmp:
            path = fixtures.generate("short", Path(tmp), replace(SPECS["F1"], audio_short_s=0.06))
            info = s2_probe.measure(path, config(), "short.mp4")
        self.assertAlmostEqual(info.audio_tracks[0].end_offset_s, -0.06, delta=0.002)
        self.assertEqual(info.warnings, [])            # information for S5, not a warning

    def test_end_offset_is_null_when_the_file_records_no_stream_lengths(self):
        # Matroska keeps stream lengths only in tags, so ffprobe reports none
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mkv"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc2=s=160x90:r=30:d=6",
                            "-f", "lavfi", "-i", "sine=d=6", "-c:v", "libx264", "-c:a", "aac",
                            str(path)], check=True)
            info = s2_probe.measure(path, config(), "clip.mkv")
        self.assertIsNone(info.audio_tracks[0].end_offset_s)
        self.assertEqual(info.validate(), [])
        self.assertEqual(s2_probe._end({"start_time": "0.5", "duration": "2"}), 2.5)
        self.assertIsNone(s2_probe._end({"start_time": "0.5"}))

    def test_U10_every_media_info_validates_and_round_trips(self):
        for name in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"):
            with self.subTest(fixture=name):
                info = measure(name)
                self.assertEqual(info.validate(), [])
                self.assertEqual(MediaInfo.from_json(info.to_json()), info)


class ProbeFailureTest(unittest.TestCase):

    def test_U8_corrupt_file_fails_cleanly_and_writes_nothing(self):
        with FakeDrive() as drive:
            name = drive.add("F9")
            pre, stage_in = run_stages(drive, name)
            with self.assertRaises(StageError) as ctx:
                s2_probe.run(pre, stage_in)
            message = str(ctx.exception)
            self.assertTrue(message.startswith("S2 Probe failed:"))
            self.assertIn("Could not read F9.mp4", message)
            self.assertNotIn(str(stage_in.local_source), message)
            self.assertFalse((stage_in.local_dir / "media_info.json").exists())
            self.assertFalse(drive.work.exists())

    def test_shorter_than_5_s_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = fixtures.Spec("3 s clip", sections=(fixtures.Section(30, 3.0),))
            path = fixtures.generate("short", Path(tmp), spec)
            with self.assertRaises(StageError) as ctx:
                s2_probe.measure(path, config(), "short.mp4")
        self.assertIn("at least 5 s", str(ctx.exception))

    def test_no_video_stream_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audio.m4a"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=1000:duration=6",
                            "-c:a", "aac", str(path)], check=True)
            with self.assertRaises(StageError) as ctx:
                s2_probe.measure(path, config(), "audio.m4a")
        self.assertIn("has no video stream", str(ctx.exception))


class ProbeOutputTest(unittest.TestCase):

    def test_writes_media_info_locally_and_to_drive(self):
        with FakeDrive() as drive:
            name = drive.add("F2")
            pre, stage_in = run_stages(drive, name)
            probe = s2_probe.run(pre, stage_in)
            self.assertEqual(probe.local_path, stage_in.local_dir / "media_info.json")
            self.assertEqual(probe.drive_path, drive.work / stage_in.match_id / "media_info.json")
            self.assertEqual(probe.local_path.read_text(), probe.drive_path.read_text())
            self.assertEqual(MediaInfo.from_json(probe.drive_path.read_text()), probe.media_info)
            self.assertEqual(sorted(p.name for p in probe.drive_path.parent.iterdir()),
                             ["media_info.json"])


class RuleUnitTest(unittest.TestCase):

    def test_choose_target_fps(self):
        for fps, want in [(24, 30), (29.97, 30), (44.9, 30), (45, 60), (59.94, 60), (120, 60)]:
            self.assertEqual(s2_probe.choose_target_fps("auto", fps), want, fps)
        self.assertEqual(s2_probe.choose_target_fps(30, 60.0), 30)

    def test_vfr_verdict_threshold(self):
        base = [Fraction(1, 30)] * 1000
        self.assertFalse(s2_probe.vfr_verdict(base)[0])
        ten_off = base[:990] + [Fraction(1, 20)] * 10         # exactly 1 % differ
        self.assertFalse(s2_probe.vfr_verdict(ten_off)[0])
        eleven_off = base[:989] + [Fraction(1, 20)] * 11
        self.assertTrue(s2_probe.vfr_verdict(eleven_off)[0])
        jitter = [Fraction(1, 30) * Fraction(105, 100)] * 500 + base[:500]   # 5 % jitter
        self.assertFalse(s2_probe.vfr_verdict(jitter)[0])

    def test_display_aspect(self):
        self.assertEqual(s2_probe.display_aspect(800, 360, "1:1", 0), "20:9")
        self.assertEqual(s2_probe.display_aspect(2400, 1080, None, 0), "20:9")
        self.assertEqual(s2_probe.display_aspect(320, 180, "1:1", 90), "9:16")
        self.assertEqual(s2_probe.display_aspect(720, 576, "16:15", 0), "4:3")
        self.assertEqual(s2_probe.display_aspect(1920, 1080, "0:1", 0), "16:9")

    def test_rotation_cw(self):
        for side, want in [(-90, 90), (90, 270), (180, 180), (-180, 180), (-89.9, 90)]:
            self.assertEqual(s2_probe.rotation_cw({"side_data_list": [{"rotation": side}]}), want)
        self.assertEqual(s2_probe.rotation_cw({}), 0)


if __name__ == "__main__":
    unittest.main()
