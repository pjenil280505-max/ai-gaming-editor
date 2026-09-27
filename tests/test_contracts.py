"""Contracts: round-trip, validation that reports every problem, schema mirrors."""

from __future__ import annotations

import copy
import json
import math
import re
import unittest

from core import contracts
from core.contracts import ContractError, MediaInfo, QcResults, RunReport, StageMarker

FP = "a" * 64


def media_info() -> dict:
    return {
        "container": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration_s": 20.0,
        "size_bytes": 696851,
        "video": {
            "codec": "h264", "width": 800, "height": 360, "rotation": 0,
            "display_aspect": "20:9", "r_frame_rate": "60/1", "avg_frame_rate": "27000/599",
            "frame_durations_s": {"min": 1 / 60, "median": 1 / 45, "max": 1 / 30, "stdev": 0.0069},
            "is_vfr": True,
            "vfr_evidence": "frame durations range 16.7–33.3 ms",
        },
        "audio_tracks": [
            {"index": 0, "codec": "aac", "sample_rate": 44100, "channels": 2, "start_offset_s": 0.0},
            {"index": 1, "codec": "aac", "sample_rate": 48000, "channels": 1, "start_offset_s": 0.5},
        ],
        "target_fps": 60,
        "audio_track_index": 0,
        "warnings": ["2 audio tracks found; using track 0 (audio.track_index)"],
    }


def stage_marker() -> dict:
    return {
        "stage": "s3_master",
        "status": "pass",
        "input_fingerprint": FP,
        "config_fingerprint": "b" * 64,
        "code_version": "e20c7d0",
        "outputs": [{"path": "master.mp4", "size_bytes": 123456}],
        "started_at": "2026-09-26T14:00:00+00:00",
        "finished_at": "2026-09-26T14:05:00Z",
    }


def run_report() -> dict:
    return {
        "run_id": "20260926T140000Z-1",
        "match_id": "20260925-2130-a1b2c3",
        "code_version": "e20c7d0",
        "config_fingerprint": FP,
        "environment": {"platform": "Linux-6.1-x86_64", "cpu_count": 2,
                        "cpu_model": "Intel(R) Xeon(R) CPU @ 2.20GHz", "gpu": None,
                        "ffmpeg_version": "4.4.2"},
        "stages": [
            {"name": "s1_stage_in", "status": "pass", "started_at": "2026-09-26T14:00:00+00:00",
             "finished_at": "2026-09-26T14:00:10+00:00", "duration_s": 10.0, "skipped": True,
             "warnings": [], "errors": []},
            {"name": "s5_qc", "status": "warn", "started_at": "2026-09-26T14:10:00+00:00",
             "finished_at": "2026-09-26T14:11:00+00:00", "duration_s": 60.0, "skipped": False,
             "warnings": ["2 black spans"], "errors": []},
        ],
        "qc": [
            {"check": "cfr", "status": "pass", "value": True, "threshold": None},
            {"check": "duration_delta_s", "status": "pass", "value": 0.02, "threshold": 0.1},
            {"check": "black_spans", "status": "warn", "value": [[0.0, 1.5]], "threshold": None},
        ],
        "totals": {"footage_minutes": 20.0, "processing_minutes": 11.0,
                   "minutes_per_footage_minute": 0.55},
    }


def qc_results() -> dict:
    return {"checks": [
        {"check": "audio_vs_video_length", "status": "pass", "value": 0.015, "threshold": 0.0399},
        {"check": "frozen_spans", "status": "warn", "value": [{"start_s": 6.0, "end_s": 9.0}],
         "threshold": 2.0},
        {"check": "loudness", "status": "pass",
         "value": {"integrated_lufs": -23.0, "true_peak_dbtp": None, "lra_lu": 0.0}, "threshold": None},
    ]}


EXAMPLES = [(MediaInfo, media_info), (StageMarker, stage_marker), (RunReport, run_report),
            (QcResults, qc_results)]


class RoundTripTest(unittest.TestCase):

    def test_json_round_trip(self):
        for cls, make in EXAMPLES:
            with self.subTest(contract=cls.CONTRACT_NAME):
                obj = cls.from_dict(make())
                self.assertIsInstance(obj, cls)
                again = cls.from_json(obj.to_json())
                self.assertEqual(again, obj)
                self.assertEqual(again.to_dict(), make())
                self.assertEqual(json.loads(obj.to_json()), make())

    def test_nested_objects_are_dataclasses(self):
        info = MediaInfo.from_dict(media_info())
        self.assertIsInstance(info.video, contracts.VideoInfo)
        self.assertIsInstance(info.video.frame_durations_s, contracts.FrameDurationStats)
        self.assertIsInstance(info.audio_tracks[1], contracts.AudioTrack)
        self.assertEqual(info.audio_tracks[1].start_offset_s, 0.5)
        report = RunReport.from_dict(run_report())
        self.assertIsInstance(report.stages[0], contracts.StageResult)
        self.assertEqual(report.qc[2].value, [[0.0, 1.5]])

    def test_valid_examples_have_no_problems(self):
        for cls, make in EXAMPLES:
            with self.subTest(contract=cls.CONTRACT_NAME):
                self.assertEqual(cls.from_dict(make()).validate(), [])

    def test_whole_numbers_accepted_for_float_fields(self):
        data = media_info()
        data["duration_s"] = 20
        info = MediaInfo.from_dict(data)
        self.assertIsInstance(info.duration_s, float)

    def test_nullable_fields(self):
        data = media_info()
        data["audio_tracks"], data["audio_track_index"] = [], None   # video-only mode
        self.assertEqual(MediaInfo.from_dict(data).audio_track_index, None)
        data = run_report()
        data["totals"]["minutes_per_footage_minute"] = None
        data["environment"]["cpu_model"] = None        # /proc/cpuinfo unreadable (DEC-029)
        self.assertEqual(RunReport.from_dict(data).validate(), [])


class ValidationTest(unittest.TestCase):

    def assertProblems(self, cls, data, expected: list[str]):
        problems = contracts.validate_data(cls, data)
        self.assertEqual(sorted(problems), sorted(expected))
        with self.assertRaises(ContractError) as ctx:
            cls.from_dict(data)
        self.assertEqual(ctx.exception.problems, problems)
        for problem in problems:
            self.assertIn(problem, str(ctx.exception))

    def test_every_missing_field_reported_at_every_level(self):
        data = media_info()
        del data["container"]
        del data["video"]["codec"]
        del data["video"]["frame_durations_s"]["stdev"]
        del data["audio_tracks"][1]["channels"]
        self.assertProblems(MediaInfo, data, [
            "container: missing",
            "video.codec: missing",
            "video.frame_durations_s.stdev: missing",
            "audio_tracks[1].channels: missing",
        ])

    def test_every_wrong_type_reported(self):
        data = stage_marker()
        data["stage"] = 3
        data["outputs"][0]["size_bytes"] = "12"
        data["outputs"].append("master.mp4")
        data["started_at"] = None
        self.assertProblems(StageMarker, data, [
            "stage: expected text, got integer 3",
            "outputs[0].size_bytes: expected integer, got text '12'",
            "outputs[1]: expected an object, got text",
            "started_at: must not be null",
        ])

    def test_bool_is_not_a_number_and_nan_is_rejected(self):
        data = media_info()
        data["size_bytes"] = True
        data["duration_s"] = math.nan
        data["video"]["is_vfr"] = 1
        self.assertProblems(MediaInfo, data, [
            "size_bytes: expected integer, got true/false True",
            "duration_s: expected finite number, got number nan",
            "video.is_vfr: expected true/false, got integer 1",
        ])

    def test_rules_enum_minimum_pattern_format(self):
        data = run_report()
        data["match_id"] = "2026-09-25-a1b2c3"
        data["config_fingerprint"] = "ABC"
        data["environment"]["cpu_count"] = 0
        data["stages"][0]["name"] = "s7_upload"
        data["stages"][0]["status"] = "ok"
        data["stages"][1]["started_at"] = "yesterday"
        data["stages"][1]["finished_at"] = "2026-09-26T14:11:00"     # no timezone
        data["qc"][0]["check"] = ""
        self.assertProblems(RunReport, data, [
            f"match_id: '2026-09-25-a1b2c3' does not match pattern {contracts.MATCH_ID}",
            f"config_fingerprint: 'ABC' does not match pattern {contracts.SHA256_HEX}",
            "environment.cpu_count: must be >= 1, got 0",
            f"stages[0].name: must be one of {list(contracts.STAGES)}, got 's7_upload'",
            "stages[0].status: must be one of ['pass', 'warn', 'fail'], got 'ok'",
            "stages[1].started_at: 'yesterday' is not an ISO 8601 timestamp with timezone",
            "stages[1].finished_at: '2026-09-26T14:11:00' is not an ISO 8601 timestamp with timezone",
            "qc[0].check: must not be empty",
        ])

    def test_qc_values_are_plain_json_with_finite_numbers(self):
        # ebur128 reports a true peak of -inf for silence; JSON can't hold it (DEC-028)
        data = qc_results()
        data["checks"][2]["value"]["true_peak_dbtp"] = -math.inf
        data["checks"][1]["value"][0]["end_s"] = math.nan
        data["checks"][0]["threshold"] = (1, 2)
        self.assertProblems(QcResults, data, [
            "checks[2].value.true_peak_dbtp: expected a finite number, got -inf",
            "checks[1].value[0].end_s: expected a finite number, got nan",
            "checks[0].threshold: expected a JSON value, got tuple",
        ])
        obj = QcResults.from_dict(qc_results())
        obj.checks[0].value = math.inf
        self.assertEqual(obj.validate(), ["checks[0].value: expected a finite number, got inf"])

    def test_unexpected_fields_reported(self):
        data = media_info()
        data["fps"] = 60
        data["video"]["bitrate"] = 1
        self.assertProblems(MediaInfo, data, ["fps: unexpected field", "video.bitrate: unexpected field"])

    def test_marker_never_records_failure(self):
        data = stage_marker()
        data["status"] = "fail"
        self.assertEqual(len(contracts.validate_data(StageMarker, data)), 1)

    def test_no_marker_for_always_run_stages(self):
        self.assertEqual(contracts.MARKER_STAGES,
                         ("s2_probe", "s3_master", "s4_analysis", "s5_qc"))
        for stage in ("s0_preflight", "s1_stage_in", "s6_stage_out"):
            data = stage_marker()
            data["stage"] = stage
            with self.subTest(stage=stage):
                self.assertProblems(StageMarker, data, [
                    f"stage: must be one of {list(contracts.MARKER_STAGES)}, got '{stage}'"])

    def test_output_paths_must_be_relative_posix(self):
        good = ["master.mp4", "stages/s3_master.done.json", "logs/..hidden"]
        bad = ["/content/work/master.mp4", "../master.mp4", "logs/../../x", "logs\\s3.txt", ""]
        for path in good + bad:
            data = stage_marker()
            data["outputs"][0]["path"] = path
            with self.subTest(path=path):
                self.assertEqual(contracts.validate_data(StageMarker, data) == [], path in good)

    def test_time_order(self):
        data = stage_marker()
        data["finished_at"] = "2026-09-26T13:59:59+00:00"
        self.assertProblems(StageMarker, data, [
            "finished_at: 2026-09-26T13:59:59+00:00 is before started_at 2026-09-26T14:00:00+00:00"])
        data = run_report()
        data["stages"][1]["finished_at"] = "2026-09-26T14:09:00+00:00"
        self.assertEqual(len(contracts.validate_data(RunReport, data)), 1)

    def test_chosen_audio_track_must_exist(self):
        data = media_info()
        data["audio_track_index"] = 2
        self.assertProblems(MediaInfo, data, [
            "audio_track_index: 2 is not the index of any entry in audio_tracks"])
        data["audio_track_index"] = None
        self.assertProblems(MediaInfo, data, [
            "audio_track_index: must name a track when audio_tracks is not empty"])
        data = media_info()
        data["audio_tracks"][1]["index"] = 0
        self.assertProblems(MediaInfo, data, ["audio_tracks: index values must be unique"])

    def test_validate_on_badly_built_object(self):
        info = MediaInfo.from_dict(media_info())
        info.video.width = "800"
        info.target_fps = 25
        self.assertEqual(sorted(info.validate()), [
            "target_fps: must be one of [30, 60], got 25",
            "video.width: expected integer, got text '800'",
        ])

    def test_from_json_rejects_bad_json_and_non_objects(self):
        for text in ["{not json", "[]", "null"]:
            with self.subTest(text=text):
                with self.assertRaises(ContractError):
                    RunReport.from_json(text)

    def test_many_problems_all_reported(self):
        data = run_report()
        for key in list(data):
            data[key] = None
        problems = contracts.validate_data(RunReport, data)
        self.assertEqual(len(problems), len(data))


class SchemaMirrorTest(unittest.TestCase):

    def test_committed_schemas_match_contracts(self):
        for cls in contracts.CONTRACTS:
            with self.subTest(schema=cls.CONTRACT_FILE):
                path = contracts.SCHEMA_DIR / cls.CONTRACT_FILE
                self.assertTrue(path.is_file(), f"missing {path}; run python -m core.contracts")
                self.assertEqual(path.read_text(encoding="utf-8"), contracts.schema_text(cls),
                                 f"{path.name} is stale; run python -m core.contracts")

    def test_schema_shape(self):
        schema = contracts.json_schema(MediaInfo)
        self.assertEqual(schema["required"], list(media_info()))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["audio_track_index"]["type"], ["integer", "null"])
        video = schema["properties"]["video"]
        self.assertEqual(video["required"], list(media_info()["video"]))
        self.assertEqual(schema["properties"]["audio_tracks"]["items"]["required"],
                         list(media_info()["audio_tracks"][0]))

    def test_schema_patterns_compile(self):
        def walk(node):
            if isinstance(node, dict):
                if "pattern" in node:
                    re.compile(node["pattern"])
                for value in node.values():
                    walk(value)
        for cls in contracts.CONTRACTS:
            walk(contracts.json_schema(cls))

    def test_examples_cover_every_field(self):
        # keeps the round-trip examples honest when a field is added
        for cls, make in EXAMPLES:
            self.assertEqual(list(make()), contracts.json_schema(cls)["required"])


if __name__ == "__main__":
    unittest.main()
