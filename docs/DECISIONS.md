# Decision log

- **DEC-001** Build path: Claude Code cloud sessions + GitHub; Colab pulls the code at a pinned tag.
- **DEC-002** Master keeps original audio levels; loudness is measured in Phase 0 and applied only at render — normalising early would flatten the crowd-noise spikes Phase 4 depends on.
- **DEC-003** Phase 0 uses only Python stdlib (unittest, dataclasses), PyYAML and FFmpeg, so tests run anywhere, including offline.
- **DEC-004** No footage, audio or large binaries in the repo, ever.
