"""Every fixture generates, and ffprobe/ffmpeg confirm its defining property."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import fixtures
import measure
from fixtures import PERIOD_S, SPECS

FRAME_DURATION_TOLERANCE_S = 1e-4
BEEP_ONSET_TOLERANCE_S = 0.002       # well inside 1 frame at 60 fps (16.7 ms)
DURATION_TOLERANCE_S = 0.1


def tearDownModule():
    if fixtures.GENERATION_SECONDS:
        times = ", ".join(f"{k} {v:.2f}s" for k, v in fixtures.GENERATION_SECONDS.items())
        total = sum(fixtures.GENERATION_SECONDS.values())
        print(f"\nfixture generation: {total:.2f} s total ({times})", file=sys.stderr)


class FixtureTest(unittest.TestCase):

    # ---- checks shared by every readable fixture, driven by fixtures.SPECS

    def check_fixture(self, name: str) -> dict:
        spec = SPECS[name]
        path = fixtures.path(name)
        self.assertTrue(path.is_file(), f"{name} was not generated")
        info = measure.probe(path)

        videos = measure.streams(info, "video")
        self.assertEqual(len(videos), 1, f"{name}: video stream count")
        video = videos[0]
        self.assertEqual(video["codec_name"], spec.vcodec)
        self.assertEqual((video["width"], video["height"]), (spec.width, spec.height))
        self.assertEqual(measure.rotation_cw(video), spec.rotation_cw)
        self.assertAlmostEqual(float(info["format"]["duration"]), spec.duration_s,
                               delta=DURATION_TOLERANCE_S)

        audios = measure.streams(info, "audio")
        self.assertEqual(
            [(a["codec_name"], int(a["sample_rate"]), a["channels"]) for a in audios],
            [("aac", t.sample_rate, t.channels) for t in spec.tracks])

        self.check_frame_rates(name, path)
        self.check_flashes(name, path)
        for index, track in enumerate(spec.tracks):
            self.check_beeps(name, path, index, track)
        return info

    def check_frame_rates(self, name: str, path: Path) -> None:
        spec = SPECS[name]
        durations = measure.video_frame_durations(path)
        self.assertEqual(len(durations) + 1, spec.frame_count, f"{name}: frame count")
        expected = {1 / s.fps for s in spec.sections}
        stray = [d for d in durations
                 if min(abs(d - e) for e in expected) > FRAME_DURATION_TOLERANCE_S]
        self.assertEqual(stray, [], f"{name}: frame durations outside {sorted(expected)}")
        for section in spec.sections:
            count = sum(1 for d in durations if abs(d - 1 / section.fps) <= FRAME_DURATION_TOLERANCE_S)
            # the last frame of a section has no successor inside it
            self.assertGreaterEqual(count, round(section.fps * section.duration_s) - 1,
                                    f"{name}: frames at {section.fps} fps")

    def check_flashes(self, name: str, path: Path) -> None:
        spec = SPECS[name]
        expected = [k * PERIOD_S for k in range(int(spec.duration_s // PERIOD_S))]
        found = measure.flash_times(path)
        self.assertEqual(len(found), len(expected), f"{name}: flash frames {found}")
        for got, want in zip(found, expected):
            self.assertAlmostEqual(got, want, delta=1e-3, msg=f"{name}: flash time")

    def check_beeps(self, name: str, path: Path, index: int, track: fixtures.Track) -> None:
        spec = SPECS[name]
        audio = measure.AudioTrack(path, index)
        self.assertGreater(audio.peak, 0, f"{name} track {index}: silent")
        expected = [track.phase_s + k * PERIOD_S for k in range(int(spec.duration_s // PERIOD_S) + 1)]
        expected = [t for t in expected if spec.audio_offset_s <= t < spec.duration_s - fixtures.BEEP_S]
        for t in expected:
            onset = audio.onset(t)
            self.assertIsNotNone(onset, f"{name} track {index}: no beep near {t} s")
            self.assertAlmostEqual(onset, t, delta=BEEP_ONSET_TOLERANCE_S,
                                   msg=f"{name} track {index}: beep onset")
            self.assertAlmostEqual(audio.frequency(t + 0.01, t + 0.09), track.beep_hz,
                                   delta=track.beep_hz * 0.02, msg=f"{name} track {index}: tone")
            quiet_until = min(t + PERIOD_S - 0.05, spec.duration_s)
            self.assertLess(audio.max_level(t + fixtures.BEEP_S + 0.05, quiet_until),
                            0.05 * audio.peak, f"{name} track {index}: sound between beeps after {t} s")

    # ---- one test per fixture

    def test_F1_constant_30fps_h264_aac(self):
        info = self.check_fixture("F1")
        video = measure.streams(info, "video")[0]
        self.assertEqual(video["r_frame_rate"], "30/1")
        self.assertEqual(video["avg_frame_rate"], "30/1")

    def test_F2_variable_frame_rate(self):
        self.check_fixture("F2")
        durations = measure.video_frame_durations(fixtures.path("F2"))
        self.assertGreater(max(durations) - min(durations), 0.01, "F2 should be clearly VFR")

    def test_F3_rotation_90(self):
        info = self.check_fixture("F3")
        self.assertEqual(measure.rotation_cw(measure.streams(info, "video")[0]), 90)

    def test_F4_no_audio(self):
        info = self.check_fixture("F4")
        self.assertEqual(measure.streams(info, "audio"), [])

    def test_F5_two_audio_tracks(self):
        info = self.check_fixture("F5")
        self.assertEqual(len(measure.streams(info, "audio")), 2)

    def test_F6_hevc(self):
        info = self.check_fixture("F6")
        self.assertEqual(measure.streams(info, "video")[0]["codec_name"], "hevc")

    def test_F7_audio_starts_half_second_late(self):
        info = self.check_fixture("F7")
        video = measure.streams(info, "video")[0]
        audio = measure.streams(info, "audio")[0]
        self.assertAlmostEqual(float(video["start_time"]), 0.0, delta=1e-3)
        # The AAC encoder's priming frame (1024 samples) is not trimmed when the
        # stream starts late, so start_time reads up to ~2 AAC frames early; the
        # audible content (checked by check_beeps above) starts at exactly 0.5 s.
        start = float(audio["start_time"])
        rate = int(audio["sample_rate"])
        self.assertGreaterEqual(start, 0.5 - 2048 / rate)
        self.assertLessEqual(start, 0.5 + 1e-3)

    def test_F8_20_by_9(self):
        info = self.check_fixture("F8")
        self.assertEqual(measure.streams(info, "video")[0]["display_aspect_ratio"], "20:9")

    def test_F9_truncated_is_unreadable(self):
        path = fixtures.path("F9")
        self.assertGreater(path.stat().st_size, 0)
        result = measure.ffprobe(path)
        self.assertNotEqual(result.returncode, 0, "ffprobe should reject the truncated file")
        self.assertTrue(result.stderr.strip(), "ffprobe should say why it failed")

    @unittest.skipUnless(fixtures.slow_enabled(), "F10 is generated only when RUN_SLOW=1")
    def test_F10_ten_minutes_160x90(self):
        self.check_fixture("F10")

    def test_fixtures_live_in_temp_dir(self):
        path = fixtures.path("F1")
        self.assertTrue(path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        repo = Path(__file__).resolve().parent.parent
        self.assertFalse(path.resolve().is_relative_to(repo))

    def test_slow_fixture_refused_without_run_slow(self):
        if fixtures.slow_enabled():
            self.skipTest("RUN_SLOW=1 is set")
        with self.assertRaises(fixtures.FixtureError):
            fixtures.path("F10")

    def test_every_spec_has_a_test(self):
        names = {n.split("_")[1] for n in dir(self) if n.startswith("test_F")}
        self.assertEqual(names, set(SPECS))


if __name__ == "__main__":
    unittest.main()
