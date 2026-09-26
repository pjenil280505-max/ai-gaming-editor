# Phase 0 — Foundation pipeline

Objective: any Android recording → verified constant-frame-rate master + analysis copy + report; resumable after Colab disconnects; operable entirely from Android.

## Drive layout

- `/AIEditor/inbox` — raw uploads; never modified or deleted in Phase 0
- `/AIEditor/work/<match_id>/` — `master.mp4`, `analysis.mp4`, `analysis.wav`, `media_info.json`, `stages/<stage>.done.json`, `run_report.json`, `summary.txt`, `logs/`
- `/AIEditor/outputs`, `/labels`, `/assets` — later phases

## Config (configs/pipeline.yaml)

- **paths:** `drive_root`, `inbox`, `work`, `local_work`
- **video:** `target_fps` auto|30|60 (auto snaps to 30 or 60 from measured median frame rate); `master_codec` h264; `master_crf` 18; `master_preset` veryfast
- **analysis:** `width` 640; `fps` 10; `audio_rate` 16000; `audio_channels` 1
- **audio:** `track_index` 0; `master_sample_rate` 48000
- **qc:** `duration_tolerance_s` 0.1; `av_sync_tolerance_frames` 1
- **loudness:** `target_lufs` -14 (applied at render time only; Phase 0 only measures)

Config fingerprint = hash of the keys each stage actually uses. Path defaults: DEC-005; which keys each stage uses: DEC-006.

## Match ID

`<YYYYMMDD-HHMM from file mtime>-<6 hex chars of hash(size + first 8 MB + last 8 MB)>`

## Stages

- **S0 Preflight:** ffmpeg/ffprobe present + versions; CPU count; GPU name; GPU encoder availability (record only); free local disk ≥ 3× input size; Drive mounted; config valid. Fail with a plain-English message.
- **S1 Stage-in:** compute match_id; copy raw file to local disk; verify size. On resume, copy finished outputs back from the Drive work dir instead of recomputing.
- **S2 Probe:** write `media_info.json` — container, duration, size; video codec, width, height, rotation, display aspect (e.g. 20:9), r_frame_rate, avg_frame_rate, frame-duration stats (min/median/max/stdev from packet timestamps), is_vfr + evidence; every audio track (codec, rate, channels, start offset); chosen target_fps; chosen audio track; warnings. Fail if: no video stream, unreadable, or duration < 5 s. No audio → video-only mode + warning. More than one audio track → configured track + warning.
- **S3 Master:** constant frame rate at target_fps; rotation applied to pixels and rotation metadata cleared; timestamps start at 0 with audio start offset corrected; audio resampled to 48 kHz AAC at ORIGINAL levels (no loudness normalisation — DEC-002). Re-probe output to confirm constant frame durations.
- **S4 Analysis copy:** from master — video at width 640 (aspect kept), 10 fps; audio mono 16 kHz WAV; duration within one analysis frame of master.
- **S5 QC v0** (each pass/warn/fail): CFR confirmed on master; |master − source| duration ≤ 0.1 s; master audio vs video duration ≤ 1 frame; black and frozen spans listed (scanned on analysis copy); loudness measured (integrated LUFS, true peak, LRA) and stored, not applied.
- **S6 Stage-out:** after every stage, copy its outputs + marker to the Drive work dir; at the end write `run_report.json` + `summary.txt`.

## Resume rule

Skip a stage only if its marker exists AND input fingerprint, config fingerprint and code version match AND every listed output exists at its recorded size. Otherwise that stage and all later stages re-run. Markers are written only after outputs are complete.

Exception: S0 Preflight runs on every run and never writes a marker, because its environment checks (Drive mounted, free disk, FFmpeg) must be fresh after a Colab disconnect (DEC-010).

## Contracts

Field-level details (names, types, allowed values): DEC-007 and `schemas/*.json`.

- **media_info.json** — fields as in S2.
- **Stage marker** — stage, status, input_fingerprint, config_fingerprint, code_version (git SHA), outputs [{path, size_bytes}], started_at, finished_at.
- **run_report.json** — run_id, match_id, code_version, config_fingerprint, environment {platform, cpu_count, gpu, ffmpeg_version}, stages [{name, status, started_at, finished_at, duration_s, skipped, warnings, errors}], qc [{check, status, value, threshold}], totals {footage_minutes, processing_minutes, minutes_per_footage_minute}.

## Notebook (notebooks/run_pipeline.ipynb)

- **C1** mount Drive + S0.
- **C2** clone/pull a pinned tag using GITHUB_TOKEN from Colab Secrets.
- **C3** install PyYAML if missing.
- **C4** form: choose /inbox file, target_fps override, force re-run.
- **C5** run with live progress %.
- **C6** summary table + Drive paths.
- **C7** self-test: run the unit tests on Colab's FFmpeg.

## Test fixtures

Generated at test time, never committed; each has a white flash + 1 kHz beep every 5 s. Details: DEC-008.

- **F1** constant 30 fps, 20 s, H.264 + AAC, 320x180
- **F2** variable frame rate: sections at 60/45/30 fps, 20 s
- **F3** rotation metadata 90°
- **F4** no audio
- **F5** two audio tracks
- **F6** HEVC video
- **F7** audio starts 0.5 s after video
- **F8** 20:9 aspect (e.g. 800x360)
- **F9** truncated/corrupt file
- **F10** 10 min at 160x90 for resume/timing tests (only when RUN_SLOW=1)

## Unit tests

- **U1** probe reports correct dimensions/fps/tracks for every fixture.
- **U2** F1 → constant, F2 → variable.
- **U3** F2 master has constant frame durations.
- **U4** in F2 and F7 masters, flash frame and beep onset within 1 frame.
- **U5** F3 output upright, rotation cleared.
- **U6** F4 completes video-only with warning.
- **U7** F5 uses configured track + warning.
- **U8** F9 fails cleanly with readable message, no partial markers.
- **U9** resume skips done stages; config change re-runs affected stages; deleted output re-runs its stage.
- **U10** media_info, marker and run_report validate against contracts.

## Acceptance (owner's real footage, on Colab)

- **A1** 3 full matches end-to-end from Android.
- **A2** constant frame rate confirmed.
- **A3** duration within ±0.1 s.
- **A4** audio/video lengths within 1 frame + owner spot-checks 3 kick sounds.
- **A5** deliberate disconnect during S3 → re-run skips S1–S2.
- **A6** processing speed recorded.
- **A7** report saved to `docs/reports/phase0.md`.

## Increments

- **0.1** docs, layout, config loader, contracts, fixture generator + their tests
- **0.2** S0–S2 + notebook C1–C4 (owner runs clip 1, sends media_info.json)
- **0.3** S3–S4 (owner watches clip 1 master)
- **0.4** resume logic + run report (owner does disconnect test)
- **0.5** S5 QC (owner runs 3 clips)
- **0.6** 3 full matches + Phase 0 report → Phase 0 done

## Decisions to settle by measurement

- **D1** If S3 takes > 1.5× real time on Colab, Phase 5 renders only kept segments from source.
- **D2** Match recording fps vs always 30.
- **D3** Master CRF vs Drive space.
- **D4** 640 px may be too small for score digits → Phase 3 may add a high-res scoreboard crop.
- **D5** Use GPU encoding only if available and quality holds.

## Definition of done

All unit tests pass in Claude Code and on Colab (C7); A1–A7 pass; phase0 report saved; owner confirms the Android flow works.
