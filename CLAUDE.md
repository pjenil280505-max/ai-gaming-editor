# AI Gaming Content Editor — working rules

Owner works from Android only. Pipeline runs on Google Colab; storage is Google Drive; code lives here; no laptop.

1. Build only the increment requested. Never start the next increment unasked.
2. Never claim anything "works" without test evidence (test output or eval numbers). Otherwise say "untested".
3. Every change ships with tests: Python unittest, run with `python -m unittest discover -s tests -v`.
4. Phase 0 dependencies: Python 3.10+ stdlib, PyYAML, FFmpeg/ffprobe CLI. Any other dependency needs a DECISIONS.md entry and owner approval.
5. No paid APIs. Pipeline code makes no network calls; it reads/writes the mounted Google Drive only.
6. Game-specific logic lives only in `games/<game>/`. `core/` never imports from `games/`. eFootball first; BGMI untouched until eFootball passes Phase 9.
7. Never commit video, audio or large binaries. Test media is generated at test time in temp dirs.
8. Contracts connect everything: `media_info.json`, stage markers, `run_report.json` (Phase 0); `match_timeline.json` and `edit_plan.json` (later). Contract changes need a DECISIONS.md entry.
9. Every pipeline stage must be resumable per the resume rule in `docs/PHASE0.md`.
10. Human review before publishing; no automatic YouTube upload.
11. Code must run on Google Colab (Ubuntu, Python 3.10+). No OS-specific paths.
12. When the spec is ambiguous, ask; record the answer in DECISIONS.md.
13. Any behaviour change updates the matching `docs/PHASE*.md` in the same PR.
14. End every session with: files changed; test command + counts; what is untested; questions. Open a PR; never merge.
