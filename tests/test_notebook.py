"""notebooks/run_pipeline.ipynb: structure, and cells C2–C4 executed outside Colab."""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from core import run

REPO = Path(__file__).resolve().parent.parent
NOTEBOOK = REPO / "notebooks" / "run_pipeline.ipynb"


def cells() -> list[str]:
    nb = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def cell(title_prefix: str) -> str:
    return next(c for c in cells() if c.startswith(f"#@title {title_prefix} "))


@contextlib.contextmanager
def fake_colab(token="tok-123"):
    """Stub google.colab.userdata; restore cwd, sys.path and core modules afterwards."""
    userdata = types.ModuleType("google.colab.userdata")

    def get(key):
        if token is None:
            raise KeyError(key)
        return token

    userdata.get = get
    colab = types.ModuleType("google.colab")
    colab.userdata = userdata
    google = types.ModuleType("google")
    google.colab = colab
    saved_modules = {k: v for k, v in sys.modules.items() if k == "core" or k.startswith("core.")}
    saved = (os.getcwd(), list(sys.path), {k: sys.modules.get(k) for k in
                                            ("google", "google.colab", "google.colab.userdata")})
    sys.modules.update({"google": google, "google.colab": colab, "google.colab.userdata": userdata})
    try:
        yield
    finally:
        os.chdir(saved[0])
        sys.path[:] = saved[1]
        for key, module in saved[2].items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module
        sys.modules.update(saved_modules)


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


def local_origin(tmp: Path) -> tuple[Path, Path, str]:
    """A local git repo tagged v-one standing in for GitHub, and C2 pointed at it."""
    origin = tmp / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    (origin / "VERSION").write_text("one")
    git("add", ".", cwd=origin)
    git("commit", "-q", "-m", "one", cwd=origin)
    git("tag", "v-one", cwd=origin)
    clone = tmp / "clone"
    source = re.sub(r'^tag = "[^"]*"', 'tag = "v-one"', cell("C2"), count=1, flags=re.M)
    source = (source
              .replace('"https://github.com/pjenil280505-max/ai-gaming-editor.git"', repr(str(origin)))
              .replace('"/content/ai-gaming-editor"', repr(str(clone))))
    return origin, clone, source


class StructureTest(unittest.TestCase):

    def test_cells_c1_to_c4_in_order_and_valid_python(self):
        titles = [c.splitlines()[0] for c in cells()]
        self.assertEqual([t.split(" · ")[0] for t in titles],
                         ["#@title C1", "#@title C2", "#@title C3", "#@title C4"])
        for source in cells():
            ast.parse(source)

    def test_c2_uses_optional_colab_secret_and_never_prints_it(self):
        c2 = cell("C2")
        self.assertIn('userdata.get("GITHUB_TOKEN")', c2)
        self.assertNotRegex(c2, r"print\([^)]*\{(token|auth)\}")
        self.assertIn('message.replace(secret, "***")', c2)

    def test_c4_form_matches_runner(self):
        c4 = cell("C4")
        self.assertIn(f"#@param {json.dumps(list(run.TARGET_FPS_CHOICES))}", c4)
        for name in ("inbox_listing", "run_to_probe"):
            self.assertIn(f"run.{name}(", c4)
            self.assertTrue(callable(getattr(run, name)))


class ExecuteCellsTest(unittest.TestCase):

    def exec_cell(self, source: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(source, "<cell>", "exec"), {"__name__": "__cell__"})
        return out.getvalue()

    def test_c2_clones_then_moves_to_a_new_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin, clone, source = local_origin(Path(tmp))
            with fake_colab():
                out = self.exec_cell(source)
            self.assertIn("Code ready: v-one", out)
            self.assertIn("using GITHUB_TOKEN", out)
            self.assertEqual((clone / "VERSION").read_text(), "one")

            (origin / "VERSION").write_text("two")
            git("commit", "-q", "-am", "two", cwd=origin)
            git("tag", "v-two", cwd=origin)
            with fake_colab():
                out = self.exec_cell(source.replace('tag = "v-one"', 'tag = "v-two"'))
            self.assertIn("Code ready: v-two", out)
            self.assertEqual((clone / "VERSION").read_text(), "two")

            with fake_colab(), self.assertRaises(SystemExit) as ctx:
                self.exec_cell(source.replace('tag = "v-one"', 'tag = "v-missing"'))
            self.assertIn("git failed", str(ctx.exception.code))

    def test_c2_works_without_secret_for_public_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin, clone, source = local_origin(Path(tmp))
            with fake_colab(token=None):
                out = self.exec_cell(source)
            self.assertIn("Code ready: v-one", out)
            self.assertNotIn("GITHUB_TOKEN", out)
            self.assertEqual((clone / "VERSION").read_text(), "one")

    def test_c3_reports_installed_pyyaml(self):
        self.assertIn("already installed", self.exec_cell(cell("C3")))

    def test_c4_lists_inbox_or_reports_stage_error(self):
        # Outside Colab the default config's Drive paths do not exist.
        self.assertIn("not found", self.exec_cell(cell("C4")))
        out = self.exec_cell(cell("C4").replace('recording = ""', 'recording = "clip.mp4"'))
        self.assertIn("S0 Preflight failed:", out)
        self.assertIn("Run cell C1", out)


if __name__ == "__main__":
    unittest.main()
