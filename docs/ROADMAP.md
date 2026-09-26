# Roadmap (approved)

Principles: build incrementally; each phase closes with an eval report (numbers + worst failures) in `docs/reports/`; held-out test matches are never used for tuning; any metric regression blocks the next phase; prefer deterministic open-source processing; AI APIs only later, behind `edit_plan.json`.

- **Phase 0 Foundation pipeline** — see [PHASE0.md](PHASE0.md)
- **Phase 1 Ground truth + evaluation harness** — label format (goals, kickoff, half-time, full-time, replays, menus, big chances, saves), label-helper notebook (one thumbnail per second), precision/recall/timing-error scripts; 10 dev + 5 held-out matches
- **Phase 2 Screen-state segmentation** — every second labelled live / replay / menu-loading / cutscene / black; first deliverable: clean match video; target ≥95% per-second accuracy, boundaries ±1 s
- **Phase 3 Scoreboard + goals** — digit-glyph matching first, OCR fallback chosen by benchmark; goals, kickoff, HT, FT; target recall and precision ≥98%, ±2 s
- **Phase 4 Important moments** — crowd/commentary energy, in-game commentary keyword spotting; target ≥80% big chances found, ≤25% judged boring
- **Phase 5 Rough cut + review loop** — rules engine → `edit_plan.json`, replay de-duplication, FFmpeg render, approve/reject logging from day one
- **Phase 6 Professional edit layer** — hooks, zooms, slow motion, speed ramps, transitions, 2–3 genuinely different styles; avoid over-editing
- **Phase 7 Audio + captions** — licensed music/SFX with licence log, ducking, loudness normalisation at render only, in-game menu music muted, captions only if owner commentates
- **Phase 8 Shorts** — 9:16 with blurred background, top moments
- **Phase 9 Packaging + final QC** — templated titles/descriptions, chapters, thumbnail candidates, final QC checks
- **Phase 10 Personal style learning** — from approve/reject log
- **Phase 11 YouTube Analytics learning** — retention drop-offs mapped to `edit_plan` segments
- **Phase 12 BGMI module** — same interface, separate folder

Content status: eFootball Terms of Use (Aug 2026) grant a personal, non-commercial licence; no eFootball-specific video guideline found; monetisation needs Konami confirmation. Use only music/SFX with a logged licence.
