"""S1 Stage-in: match_id rule (UTC mtime + hash) and the verified local copy."""

from __future__ import annotations

import hashlib
import os
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from core import contracts, files, s0_preflight, s1_stage_in
from core.errors import StageError
from fake_drive import FakeDrive

MIB = 1024 * 1024
MATCH_TIME = datetime(2026, 9, 25, 21, 30, 59, tzinfo=timezone.utc).timestamp()


def expected_id(path: Path) -> str:
    """The PHASE0 rule, computed independently of core/."""
    data = path.read_bytes()
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y%m%d-%H%M")
    digest = hashlib.sha256(str(len(data)).encode() + data[:8 * MIB] + data[-8 * MIB:]).hexdigest()
    return f"{stamp}-{digest[:6]}"


class MatchIdTest(unittest.TestCase):

    def setUp(self):
        self.drive = FakeDrive()
        self.addCleanup(self.drive.cleanup)

    def make(self, name: str, data: bytes) -> Path:
        path = self.drive.inbox / name
        path.write_bytes(data)
        os.utime(path, (MATCH_TIME, MATCH_TIME))
        return path

    def test_format_and_utc_timestamp(self):
        path = self.make("a.mp4", b"x" * 1000)
        mid = s1_stage_in.match_id(path)
        self.assertRegex(mid, contracts.MATCH_ID)
        self.assertTrue(mid.startswith("20260925-2130-"), mid)
        self.assertEqual(mid, expected_id(path))

    def test_large_file_uses_size_first_and_last_8_mib(self):
        data = bytearray(os.urandom(20 * MIB))
        path = self.make("big.mp4", bytes(data))
        base = s1_stage_in.match_id(path)
        self.assertEqual(base, expected_id(path))

        data[10 * MIB] ^= 0xFF                      # middle byte: not hashed by design
        self.assertEqual(s1_stage_in.match_id(self.make("big.mp4", bytes(data))), base)
        data[0] ^= 0xFF                             # first byte
        first = s1_stage_in.match_id(self.make("big.mp4", bytes(data)))
        self.assertNotEqual(first, base)
        data[-1] ^= 0xFF                            # last byte
        self.assertNotEqual(s1_stage_in.match_id(self.make("big.mp4", bytes(data))), first)
        self.assertNotEqual(s1_stage_in.match_id(self.make("big.mp4", bytes(data) + b"\0")), first)

    def test_recording_time_in_file_name_wins_over_mtime(self):
        # the owner's recorder names files like this (clip 1, 2026-09-26)
        name = "Record_2026-08-21-21-54-38_dacb6cb66c1ceaabd92782c8217a74fb.mp4"
        path = self.make(name, b"x" * 1000)
        mid, source = s1_stage_in.match_id_with_source(path)
        self.assertEqual(mid, "20260821-2154-" + expected_id(path).rsplit("-", 1)[1])
        self.assertEqual(source, s1_stage_in.FROM_NAME)
        self.assertRegex(mid, contracts.MATCH_ID)

    def test_no_or_invalid_time_in_name_falls_back_to_utc_mtime(self):
        for name in ("a.mp4", "Record_2026-13-45-99-99-99_x.mp4", "x12026-08-21-21-54-381.mp4"):
            with self.subTest(name=name):
                path = self.make(name, b"x" * 1000)
                mid, source = s1_stage_in.match_id_with_source(path)
                self.assertEqual(mid, expected_id(path))
                self.assertTrue(mid.startswith("20260925-2130-"))
                self.assertEqual(source, s1_stage_in.FROM_MTIME)

    def test_recording_time_parsing(self):
        parse = s1_stage_in.recording_time
        self.assertEqual(parse("Record_2026-08-21-21-54-38_x.mp4"), datetime(2026, 8, 21, 21, 54, 38))
        self.assertIsNone(parse("Match 1.MP4"))
        self.assertIsNone(parse("2026-08-21.mp4"))
        self.assertEqual(parse("a_2026-02-30-10-00-00_b_2026-03-01-10-00-00.mp4"),
                         datetime(2026, 3, 1, 10, 0, 0))     # first valid one

    def test_same_content_and_time_same_id(self):
        a = self.make("a.mp4", b"same bytes")
        b = self.make("b.mp4", b"same bytes")
        self.assertEqual(s1_stage_in.match_id(a), s1_stage_in.match_id(b))


class StageInTest(unittest.TestCase):

    def setUp(self):
        self.drive = FakeDrive()
        self.addCleanup(self.drive.cleanup)
        self.name = self.drive.add("F1", "Match 1.MP4")
        self.pre = s0_preflight.run(self.name, None, self.drive.config_path)

    def test_copies_verifies_and_leaves_inbox_untouched(self):
        source = self.drive.inbox / self.name
        before = (source.stat().st_size, source.stat().st_mtime_ns)
        seen = []
        result = s1_stage_in.run(self.pre, progress=seen.append)
        self.assertTrue(result.copied)
        self.assertEqual(result.match_id, expected_id(source))
        self.assertEqual(result.time_source, s1_stage_in.FROM_MTIME)
        self.assertEqual(result.local_dir, self.drive.local_work / result.match_id)
        self.assertEqual(result.local_source.name, "source.mp4")
        self.assertEqual(result.local_source.read_bytes(), source.read_bytes())
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual((source.stat().st_size, source.stat().st_mtime_ns), before)
        self.assertEqual([p.name for p in result.local_dir.iterdir()], ["source.mp4"])

    def test_reuses_same_size_copy_unless_forced(self):
        s1_stage_in.run(self.pre)
        self.assertFalse(s1_stage_in.run(self.pre).copied)
        self.assertTrue(s1_stage_in.run(self.pre, force=True).copied)

    def test_size_mismatch_fails_and_cleans_up(self):
        def short_copy(src, dst, total, progress):
            dst.write_bytes(src.read_bytes()[:-10])
        with mock.patch.object(files, "_copy_chunks", short_copy):
            with self.assertRaises(StageError) as ctx:
                s1_stage_in.run(self.pre)
        self.assertIn("may still be uploading", str(ctx.exception))
        self.assertTrue(str(ctx.exception).startswith("S1 Stage-in failed:"))
        local_dir = self.drive.local_work / s1_stage_in.match_id(self.pre.source)
        self.assertEqual(list(local_dir.iterdir()), [])

    def test_read_error_is_plain_english(self):
        def broken_copy(src, dst, total, progress):
            raise OSError(5, "Input/output error")
        with mock.patch.object(files, "_copy_chunks", broken_copy):
            with self.assertRaises(StageError) as ctx:
                s1_stage_in.run(self.pre)
        self.assertRegex(str(ctx.exception), re.escape("Input/output error") + ".*Re-run")


if __name__ == "__main__":
    unittest.main()
