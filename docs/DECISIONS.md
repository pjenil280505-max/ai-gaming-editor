# Decision log

- **DEC-001** Build path: Claude Code cloud sessions + GitHub; Colab pulls the code at a pinned tag.
- **DEC-002** Master keeps original audio levels; loudness is measured in Phase 0 and applied only at render — normalising early would flatten the crowd-noise spikes Phase 4 depends on.
- **DEC-003** Phase 0 uses only Python stdlib (unittest, dataclasses), PyYAML and FFmpeg, so tests run anywhere, including offline.
- **DEC-004** No footage, audio or large binaries in the repo, ever.
- **DEC-009** Target FFmpeg is 6.1.1 (Ubuntu build `6.1.1-3ubuntu5`) on both Colab and the Claude Code cloud box; owner checked Colab on 2026-09-26. Code and tests use FFmpeg 6 options (`-fps_mode`, `-display_rotation`) with no fallbacks for older builds. S0 (increment 0.2) will record the version on every run, so a Colab image change will show up in run_report.json.

## Proposed in increment 0.1 — awaiting owner approval

The spec left these open. Each is implemented as written below so 0.1 could be
built and tested; each is easy to change before 0.2 builds on it.

- **DEC-005 (proposed)** Config path defaults. `drive_root: /content/drive/MyDrive/AIEditor` (where Colab mounts Drive); `inbox: inbox` and `work: work` are relative to `drive_root`; `local_work: /content/aieditor_work` (Colab local disk). `drive_root` and `local_work` must be absolute.
- **DEC-006 (proposed)** Which config keys each stage's fingerprint covers. Only keys that change what a stage writes: S2 `video.target_fps`, `audio.track_index`; S3 `video.master_codec`, `video.master_crf`, `video.master_preset`, `audio.master_sample_rate`; S4 all `analysis.*`; S5 all `qc.*`; S0, S1, S6 none. `paths.*` are in no fingerprint, so moving or renaming the Drive folder does not force a re-run. S3 sees target_fps and the audio track through media_info.json (its input), so changing them re-runs S2 onwards via the resume rule. `loudness.target_lufs` is used by no Phase 0 stage (render only). Fingerprint = SHA-256 of the sorted key/value JSON; `1` and `1.0` hash the same. run_report's `config_fingerprint` covers every non-path key.
- **DEC-007 (proposed)** Contract details not fixed by PHASE0.md. Stage ids `s0_preflight` … `s6_stage_out`. One status vocabulary everywhere: pass / warn / fail (a marker is only pass or warn, since markers are written only after success). `video.rotation` is clockwise degrees (0/90/180/270); width/height are coded (pre-rotation) pixels. `AudioTrack.index` and `audio.track_index` count audio streams only (0 = first audio stream). Timestamps are ISO 8601 with timezone. Marker output paths are relative to the match work dir, `/`-separated, no `..`. Fingerprints are 64-char lowercase hex. `totals.minutes_per_footage_minute` is null when footage is 0. Unknown fields are rejected. schemas/*.json are generated from core/contracts.py, never hand-edited.
- **DEC-008 (proposed)** Fixture details. All fixtures are 20 s except F10 (10 min, 30 fps). F2 sections: 60 fps 0–7 s, 45 fps 7–13 s, 30 fps 13–20 s, so flashes land inside sections, not on the joins. Audio is AAC 44.1 kHz stereo, so S3's resample to 48 kHz is exercised. F5 track 1 is mono 48 kHz with 440 Hz beeps at +2.5 s, so a test can tell which track a master used. F3 uses the classic `rotate=90` meaning (display matrix −90°). F9 is the first half of an F1-style MP4, so the index (moov) is missing.
