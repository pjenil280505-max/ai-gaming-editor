"""Resume rule end to end (PHASE0 U9, A5) plus fingerprints, markers and the run plan."""

from __future__ import annotations

import os
import shutil
import unittest
from unittest import mock

import yaml

from core import resume, run, s0_preflight, s3_master, s5_qc
from core.config import stage_fingerprint
from core.contracts import QcResults, RunReport, StageMarker
from core.errors import StageError
from fake_drive import FakeDrive


def quiet_run(drive: FakeDrive, **kwargs):
    lines: list[str] = []
    result = run.run_pipeline("1", config_path=drive.config_path, log=lines.append, **kwargs)
    return result, "\n".join(lines)


def ran(result) -> list[str]:
    """Stages S2–S5 that actually ran (not skipped) in this run."""
    report = RunReport.from_json(result.report_path.read_text())
    return [s.name for s in report.stages if s.name in resume.RESUMABLE and not s.skipped]


def set_config(drive: FakeDrive, key: str, value) -> None:
    config = yaml.safe_load(drive.config_path.read_text())
    section, name = key.split(".")
    config[section][name] = value
    drive.config_path.write_text(yaml.safe_dump(config))


class ResumeTestCase(unittest.TestCase):
    fixture = "F7"

    def setUp(self):
        self.drive = FakeDrive()
        self.addCleanup(self.drive.cleanup)
        self.drive.add(self.fixture)
        self.first, _ = quiet_run(self.drive)
        self.dir = self.first.drive_dir

    def disconnect(self):
        """What a Colab disconnect does: Colab's local disk is wiped, Drive is kept."""
        shutil.rmtree(self.drive.local_work)


class FirstRunTest(ResumeTestCase):

    def test_markers_are_valid_and_match_outputs(self):
        self.assertEqual(ran(self.first), list(resume.RESUMABLE))
        version = resume.code_version()
        for stage in resume.RESUMABLE:
            with self.subTest(stage=stage):
                path = resume.marker_path(self.dir, stage)
                marker = StageMarker.from_json(path.read_text())
                self.assertEqual(marker.stage, stage)
                self.assertEqual(marker.code_version, version)
                self.assertEqual(marker.config_fingerprint, stage_fingerprint(
                    yaml.safe_load(self.drive.config_path.read_text()), stage))
                self.assertEqual(marker.input_fingerprint,
                                 resume.input_fingerprint(stage, self.dir, self.drive.inbox / "F7.mp4"))
                for output in marker.outputs:
                    out = self.dir / output.path
                    self.assertEqual(out.stat().st_size, output.size_bytes)
                    # outputs first, marker last (DEC-011)
                    self.assertLessEqual(out.stat().st_mtime_ns, path.stat().st_mtime_ns)
        outputs = {s: [o.path for o in resume.read_marker(self.dir, s).outputs] for s in resume.RESUMABLE}
        self.assertEqual(outputs, {"s2_probe": ["media_info.json"], "s3_master": ["master.mp4"],
                                   "s4_analysis": ["analysis.mp4", "analysis.wav"],
                                   "s5_qc": ["qc.json"]})

    def test_report_summary_and_logs(self):
        report = RunReport.from_json(self.first.report_path.read_text())
        self.assertEqual(report.match_id, self.first.match_id)
        self.assertEqual([s.name for s in report.stages],
                         ["s0_preflight", "s1_stage_in", "s2_probe", "s3_master", "s4_analysis",
                          "s5_qc", "s6_stage_out"])
        self.assertEqual(report.qc, QcResults.from_json((self.dir / "qc.json").read_text()).checks)
        self.assertEqual([c.check for c in report.qc], list(s5_qc.LABELS))
        self.assertEqual(report.qc, self.first.qc)
        self.assertAlmostEqual(report.totals.footage_minutes, 20 / 60, places=3)
        self.assertIsNotNone(report.totals.minutes_per_footage_minute)
        self.assertEqual(report.environment.ffmpeg_version.split()[:2], ["ffmpeg", "version"])
        self.assertEqual(report.environment.cpu_model, s0_preflight.cpu_model())
        summary = self.first.summary_path.read_text()
        self.assertIn("Result: WARN", summary)            # F7: audio starts late
        self.assertIn("Watch master.mp4", summary)
        self.assertIn(f"Colab machine: {report.environment.cpu_count} CPUs, "
                      f"{report.environment.cpu_model or 'CPU model unknown'}", summary)
        self.assertIn("QC checks:\n  Constant frame rate    pass  every frame lasts 1/30 s", summary)
        logs = sorted(p.name for p in (self.dir / "logs").iterdir())
        self.assertEqual(logs, [f"run-{report.run_id}.txt", "s3_master.ffmpeg.log",
                                "s4_analysis.ffmpeg.log", "s5_qc.loudness.ffmpeg.log",
                                "s5_qc.scan.ffmpeg.log"])
        self.assertIn("S3 Master: encoding", (self.dir / "logs" / f"run-{report.run_id}.txt").read_text())


class U9Test(ResumeTestCase):

    def test_second_run_skips_every_done_stage_and_copies_nothing(self):
        self.disconnect()
        master_mtime = (self.dir / "master.mp4").stat().st_mtime_ns
        second, log = quiet_run(self.drive)
        self.assertEqual(ran(second), [])
        self.assertEqual(second.skipped, list(resume.RESUMABLE))
        self.assertIn("the recording is not needed on local disk", log)
        self.assertFalse(self.drive.local_work.exists() and any(self.drive.local_work.rglob("source.*")))
        self.assertEqual((self.dir / "master.mp4").stat().st_mtime_ns, master_mtime)

    def test_skipped_qc_keeps_its_results_in_the_report(self):
        self.disconnect()
        second, log = quiet_run(self.drive)
        report = RunReport.from_json(second.report_path.read_text())
        self.assertEqual(report.qc, self.first.qc)
        self.assertEqual(second.qc, self.first.qc)
        self.assertIn("S5 QC: skipped (done in an earlier run); its results:", log)
        self.assertIn("Loudness", log)
        self.assertIn("QC checks:", second.summary_path.read_text())

    def test_analysis_setting_change_reruns_s4_and_s5(self):
        self.disconnect()
        set_config(self.drive, "analysis.fps", 5)
        second, log = quiet_run(self.drive)
        self.assertEqual(ran(second), ["s4_analysis", "s5_qc"])
        self.assertIn("S4 Analysis copy: runs (settings it uses changed)", log)
        self.assertIn("copying the earlier master back from Drive for S4", log)

    def test_qc_setting_change_reruns_only_s5_from_drive_copies(self):
        self.disconnect()
        set_config(self.drive, "qc.duration_tolerance_s", 0.2)
        second, log = quiet_run(self.drive)
        self.assertEqual(ran(second), ["s5_qc"])
        self.assertIn("S5 QC: runs (settings it uses changed)", log)
        self.assertIn("S5 QC: checking the master and the analysis copy ...\n"
                      "  copying the earlier master back from Drive for S5", log)
        self.assertIn("copying the earlier analysis.mp4 back from Drive for S5", log)
        self.assertEqual([c.threshold for c in second.qc if c.check == "length_vs_recording"], [0.2])
        self.assertFalse(any(self.drive.local_work.rglob("source.*")))   # recording not needed

    def test_failed_qc_check_fails_the_run_and_leaves_no_marker(self):
        set_config(self.drive, "qc.duration_tolerance_s", 0.001)     # F7's master is 4 ms longer
        with self.assertRaises(StageError) as ctx:
            quiet_run(self.drive)
        self.assertTrue(str(ctx.exception).startswith("S5 QC failed:"))
        self.assertIn("Length vs recording: master is 0.004 s longer than the recording "
                      "(limit 0.001 s). Send this message.", str(ctx.exception))
        report = RunReport.from_json((self.dir / "run_report.json").read_text())
        self.assertEqual([(s.name, s.status) for s in report.stages if s.name == "s5_qc"],
                         [("s5_qc", "fail")])
        self.assertIn("fail", [c.status for c in report.qc])
        self.assertIn("Result: FAILED at S5 QC", (self.dir / "summary.txt").read_text())
        self.assertFalse(resume.marker_path(self.dir, "s5_qc").exists())
        # the earlier passing marker is gone too, so going back to the old setting re-checks
        # instead of skipping S5 with the failed qc.json (DEC-030)
        set_config(self.drive, "qc.duration_tolerance_s", 0.1)
        second, _ = quiet_run(self.drive)
        self.assertEqual(ran(second), ["s5_qc"])
        self.assertNotIn("fail", [c.status for c in second.qc])

    def test_unreadable_qc_json_runs_s5_again(self):
        path = self.dir / "qc.json"
        path.write_text("x" * path.stat().st_size)        # same size, so S5 still looks done
        second, log = quiet_run(self.drive)
        self.assertEqual(ran(second), ["s5_qc"])
        self.assertIn("S5 QC: qc.json from the earlier run can't be read, so S5 runs again", log)
        self.assertEqual(second.qc, self.first.qc)
        self.assertEqual(second.skipped, ["s2_probe", "s3_master", "s4_analysis"])
        self.assertIsNotNone(resume.read_marker(self.dir, "s5_qc"))

    def test_master_setting_change_reruns_s3_to_s5(self):
        set_config(self.drive, "video.master_crf", 20)
        second, log = quiet_run(self.drive)
        self.assertEqual(ran(second), ["s3_master", "s4_analysis", "s5_qc"])
        self.assertIn("S4 Analysis copy: runs (an earlier stage (S3 Master) runs again)", log)

    def test_fps_override_reruns_from_s2(self):
        second, _ = quiet_run(self.drive, target_fps="60")
        self.assertEqual(ran(second), list(resume.RESUMABLE))
        self.assertEqual(second.master.fps, 60)

    def test_deleted_output_reruns_its_stage(self):
        (self.dir / "qc.json").unlink()
        second, log = quiet_run(self.drive)
        self.assertEqual(ran(second), ["s5_qc"])
        self.assertIn("qc.json is missing on Drive", log)

        (self.dir / "analysis.wav").unlink()
        third, log = quiet_run(self.drive)
        self.assertEqual(ran(third), ["s4_analysis", "s5_qc"])
        self.assertIn("analysis.wav is missing on Drive", log)

        (self.dir / "master.mp4").unlink()
        fourth, _ = quiet_run(self.drive)
        self.assertEqual(ran(fourth), ["s3_master", "s4_analysis", "s5_qc"])

    def test_output_of_wrong_size_reruns_its_stage(self):
        with open(self.dir / "analysis.mp4", "ab") as f:
            f.write(b"junk")
        second, log = quiet_run(self.drive)
        self.assertEqual(ran(second), ["s4_analysis", "s5_qc"])
        self.assertIn("analysis.mp4 on Drive is not the size recorded", log)

    def test_changed_input_reruns_the_stage_that_reads_it(self):
        info = self.dir / "media_info.json"
        text = info.read_text()
        info.write_text(text.replace('"target_fps": 30', '"target_fps": 60'))   # same size
        second, log = quiet_run(self.drive)
        self.assertEqual(ran(second), ["s3_master", "s4_analysis", "s5_qc"])
        self.assertIn("S3 Master: runs (its input changed)", log)


class A5Test(ResumeTestCase):

    def test_disconnect_during_s3_skips_s2_and_restarts_s3(self):
        # State a disconnect during S3 leaves: S2 finished (marker on Drive), S3 did not
        # (no marker, maybe a half-copied master), S4 and S5 never started, local disk wiped.
        for stage in ("s3_master", "s4_analysis", "s5_qc"):
            resume.marker_path(self.dir, stage).unlink()
        for name in ("master.mp4", "analysis.mp4", "analysis.wav", "qc.json"):
            (self.dir / name).unlink()
        (self.dir / "master.mp4.partial").write_bytes(b"half a master")
        self.disconnect()

        second, log = quiet_run(self.drive)
        self.assertEqual(ran(second), ["s3_master", "s4_analysis", "s5_qc"])
        self.assertIn("S2 Probe: skipped (done in an earlier run)", log)
        self.assertIn("copying", log)                    # S3 needs the recording again
        self.assertFalse((self.dir / "master.mp4.partial").exists())
        self.assertTrue(resume.marker_path(self.dir, "s3_master").is_file())

    def test_failed_s3_leaves_s2_marker_and_reruns_from_s3(self):
        def broken(*args, **kwargs):
            raise StageError("S3 Master", ["simulated failure"])

        with mock.patch.object(s3_master, "run", broken):
            set_config(self.drive, "video.master_crf", 21)
            with self.assertRaises(StageError):
                quiet_run(self.drive)
        report = RunReport.from_json((self.dir / "run_report.json").read_text())
        self.assertEqual([(s.name, s.status) for s in report.stages if s.name == "s3_master"],
                         [("s3_master", "fail")])
        self.assertFalse(resume.marker_path(self.dir, "s3_master").exists())    # DEC-030
        self.assertTrue(resume.marker_path(self.dir, "s2_probe").exists())
        second, _ = quiet_run(self.drive)
        self.assertEqual(ran(second), ["s3_master", "s4_analysis", "s5_qc"])


class ReasonsTest(ResumeTestCase):
    fixture = "F1"

    def test_code_change_reruns_everything(self):
        with mock.patch.object(resume, "code_version", return_value="f" * 40):
            second, log = quiet_run(self.drive)
        self.assertEqual(ran(second), list(resume.RESUMABLE))
        self.assertIn("the pipeline code changed since it last ran", log)

    def test_unknown_code_version_never_skips(self):
        with mock.patch.object(resume, "code_version", return_value=None):
            second, log = quiet_run(self.drive)
            third, _ = quiet_run(self.drive)
        self.assertEqual(ran(third), list(resume.RESUMABLE))
        self.assertIn("the code version can't be read, so nothing is skipped", log)
        self.assertEqual(resume.read_marker(self.dir, "s2_probe").code_version, "unknown")

    def test_unreadable_marker_reruns_from_that_stage(self):
        resume.marker_path(self.dir, "s3_master").write_text("{not json")
        second, log = quiet_run(self.drive)
        self.assertEqual(ran(second), ["s3_master", "s4_analysis", "s5_qc"])
        self.assertIn("its record on Drive is unreadable", log)

    def test_corrupt_media_info_of_skipped_s2_is_a_plain_error(self):
        info = self.dir / "media_info.json"
        info.write_text("x" * info.stat().st_size)        # same size, so S2 still looks done
        with self.assertRaises(StageError) as ctx:
            quiet_run(self.drive)
        self.assertIn("media_info.json from an earlier run can't be read", str(ctx.exception))
        self.assertIn("Tick force re-run", str(ctx.exception))
        forced, _ = quiet_run(self.drive, force=True)
        self.assertEqual(ran(forced), list(resume.RESUMABLE))

    def test_force_reruns_everything(self):
        second, log = quiet_run(self.drive, force=True)
        self.assertEqual(ran(second), list(resume.RESUMABLE))
        self.assertIn("runs (force re-run is ticked)", log)


class FingerprintTest(unittest.TestCase):

    def test_file_fingerprint(self):
        with FakeDrive() as drive:
            path = drive.inbox / "x.bin"
            data = bytearray(os.urandom(20 * 1024 * 1024))
            path.write_bytes(bytes(data))
            base = resume.file_fingerprint(path)
            self.assertRegex(base, r"^[0-9a-f]{64}$")
            self.assertEqual(resume.file_fingerprint(path), base)
            data[-1] ^= 0xFF
            path.write_bytes(bytes(data))
            self.assertNotEqual(resume.file_fingerprint(path), base)

    def test_clear_marker(self):
        with FakeDrive() as drive:
            resume.clear_marker(drive.work, "s5_qc")               # nothing there: no error
            path = resume.marker_path(drive.work, "s5_qc")
            path.parent.mkdir(parents=True)
            path.write_text("{}")
            resume.clear_marker(drive.work, "s5_qc")
            self.assertFalse(path.exists())
            path.mkdir()                                            # can't be deleted as a file
            with self.assertRaises(StageError) as ctx:
                resume.clear_marker(drive.work, "s5_qc")
            self.assertTrue(str(ctx.exception).startswith("S5 QC failed:"))
            self.assertIn("Could not update the stage record on Drive", str(ctx.exception))

    def test_s5_fingerprint_covers_what_s5_reads(self):
        # DEC-028: media_info.json, master.mp4 and analysis.mp4 (not the recording)
        with FakeDrive() as drive:
            names = ("media_info.json", "master.mp4", "analysis.mp4")
            for name in names:
                (drive.work / name).parent.mkdir(parents=True, exist_ok=True)
                (drive.work / name).write_bytes(name.encode())
            source = drive.inbox / "rec.mp4"
            source.write_bytes(b"recording")
            self.assertEqual(sorted(resume.input_files("s5_qc", drive.work, source)), sorted(names))
            base = resume.input_fingerprint("s5_qc", drive.work, source)
            source.write_bytes(b"another recording")
            self.assertEqual(resume.input_fingerprint("s5_qc", drive.work, source), base)
            for name in names:
                with self.subTest(changed=name):
                    (drive.work / name).write_bytes(b"changed " + name.encode())
                    self.assertNotEqual(resume.input_fingerprint("s5_qc", drive.work, source), base)
                    (drive.work / name).write_bytes(name.encode())

    def test_missing_input_gives_no_fingerprint(self):
        with FakeDrive() as drive:
            self.assertIsNone(resume.input_fingerprint("s4_analysis", drive.work, drive.inbox / "x"))

    def test_code_version_is_the_git_commit(self):
        self.assertRegex(resume.code_version() or "", r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
