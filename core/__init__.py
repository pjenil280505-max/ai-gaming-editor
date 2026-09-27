"""Pipeline core: game-agnostic code. Never imports from games/ (CLAUDE.md rule 6)."""

# Release of this code. Bump it with every increment and create the matching
# GitHub release tag v<VERSION> after merging (DEC-019); notebook C2 defaults
# to it and C5 refuses to run with a notebook from another release (DEC-027).
VERSION = "0.5.1"
