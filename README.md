# AI Gaming Content Editor

Turns Android eFootball recordings into edited YouTube videos, operated from an
Android phone: the pipeline runs on Google Colab and keeps its files on Google
Drive. Working rules are in [CLAUDE.md](CLAUDE.md), the plan in
[docs/ROADMAP.md](docs/ROADMAP.md), the current phase in
[docs/PHASE0.md](docs/PHASE0.md), decisions in [docs/DECISIONS.md](docs/DECISIONS.md).

## What exists (Phase 0, increment 0.2)

Stages S0–S2 run from the Colab notebook and save `media_info.json` to Drive.
S3–S6 (master, analysis copy, QC, report) and resume logic do not exist yet.

| Path | What it is |
| --- | --- |
| `notebooks/run_pipeline.ipynb` | Colab notebook: C1 mount Drive, C2 download code at a tag, C3 PyYAML, C4 pick a recording and run S0 → S2 |
| `core/s0_preflight.py` | S0: checks Drive, the recording, free disk, FFmpeg, config; records CPU/GPU |
| `core/s1_stage_in.py` | S1: match ID, copies the recording to Colab's local disk, verifies size |
| `core/s2_probe.py` | S2: measures the recording and writes `media_info.json` locally and to Drive |
| `core/run.py` | What notebook cell C4 calls: inbox listing, choosing a file, S0 → S2 with progress |
| `core/config.py` | Loads and validates `configs/pipeline.yaml`; config fingerprints; path resolution |
| `core/contracts.py` | `media_info.json`, stage marker and `run_report.json` contracts; `schemas/*.json` mirrors |
| `core/ffmpeg.py`, `core/errors.py` | ffmpeg/ffprobe wrappers; plain-English stage errors |
| `games/efootball/` | Empty package for eFootball-specific code |
| `tests/` | Unit tests; `fixtures.py` generates test videos F1–F10 with FFmpeg into a temp dir |
| `docs/cloud-setup.md` | Setup script for the Claude Code cloud environment (installs FFmpeg) |

## Running on Colab from Android

1. Upload a recording to **My Drive › AIEditor › inbox** (create the folders once).
2. Open the notebook at the tag you were given, e.g. for `v0.2.0`:
   `https://colab.research.google.com/github/pjenil280505-max/ai-gaming-editor/blob/v0.2.0/notebooks/run_pipeline.ipynb`
3. Tap ▶ on C1 (allow Drive access), then C2 and C3.
4. Run C4 with **recording** empty to see the numbered inbox list, type the number, run C4 again.
5. C4 prints where `media_info.json` was saved on Drive.

No GitHub token is needed while the repo is public (DEC-018).

**Creating a tag (owner, after merging an increment's PR; DEC-019).** In the phone browser open the repo on github.com → **Releases** → **Draft a new release** → **Choose a tag**, type the name Claude gave you (e.g. `v0.2.0`) and pick *Create new tag on publish* → target **main** → **Publish release**.

## Running the tests

Needs Python 3.10+, PyYAML and `ffmpeg` + `ffprobe` on the PATH. From the repo root:

```bash
python -m unittest discover -s tests -v
```

F10 (a 10-minute fixture) is skipped unless you ask for it:

```bash
RUN_SLOW=1 python -m unittest discover -s tests -v
```

To see how long fixture generation takes on a machine:

```bash
python tests/fixtures.py          # F1–F9
python tests/fixtures.py --slow   # F1–F10
```

After changing a contract in `core/contracts.py`, regenerate the schema
mirrors with `python -m core.contracts` (a test fails until you do) and add a
DECISIONS.md entry.
