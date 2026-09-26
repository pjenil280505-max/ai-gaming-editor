"""Repo layout rules from CLAUDE.md that code can check."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class LayoutTest(unittest.TestCase):

    def test_core_never_imports_games(self):
        # CLAUDE.md rule 6
        offenders = []
        for path in sorted((REPO / "core").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                offenders += [f"{path.relative_to(REPO)}: {n}" for n in names
                              if n == "games" or n.startswith("games.")]
        self.assertEqual(offenders, [])

    def test_packages_and_dirs_exist(self):
        for rel in ["core/__init__.py", "games/__init__.py", "games/efootball/__init__.py",
                    "configs/pipeline.yaml", "schemas", "notebooks", "docs/PHASE0.md"]:
            self.assertTrue((REPO / rel).exists(), rel)


if __name__ == "__main__":
    unittest.main()
