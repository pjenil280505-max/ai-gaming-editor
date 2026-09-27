"""notebooks/run_pipeline.ipynb: structure, and cells C2–C7 executed outside Colab."""

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
from unittest import mock

import yaml

from core import config, run, selftest

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


@contextlib.contextmanager
def no_drive():
    """Point the default config at a Drive folder that doesn't exist.

    C7 runs these tests on Colab, where the owner's Drive is mounted; without this
    the cells would list the real inbox and the tests would depend on it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        data = config.load_config()
        data["paths"]["drive_root"] = str(Path(tmp) / "not-mounted" / "AIEditor")
        data["paths"]["local_work"] = str(Path(tmp) / "local")
        path = Path(tmp) / "pipeline.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with mock.patch.object(config, "DEFAULT_CONFIG_PATH", path):
            yield


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

    def test_cells_c1_to_c7_in_order_and_valid_python(self):
        titles = [c.splitlines()[0] for c in cells()]
        self.assertEqual([t.split(" · ")[0] for t in titles],
                         [f"#@title C{i}" for i in range(1, 8)])
        for source in cells():
            ast.parse(source)

    def test_c2_uses_optional_colab_secret_and_never_prints_it(self):
        c2 = cell("C2")
        self.assertIn('userdata.get("GITHUB_TOKEN")', c2)
        self.assertNotRegex(c2, r"print\([^)]*\{(token|auth)\}")
        self.assertIn('message.replace(secret, "***")', c2)

    def test_c4_c5_match_runner(self):
        c4, c5 = cell("C4"), cell("C5")
        self.assertIn(f"#@param {json.dumps(list(run.TARGET_FPS_CHOICES))}", c4)
        for source, name in ((c4, "inbox_listing"), (c4, "describe_choice"), (c5, "run_pipeline")):
            self.assertIn(f"run.{name}(", source)
            self.assertTrue(callable(getattr(run, name)))
        self.assertIn("overview.text()", cell("C6"))
        self.assertIn("selftest.run()", cell("C7"))
        self.assertIn("run_self_test = False", cell("C7"))     # never runs on Run all by default


    def test_release_numbers_agree(self):
        # core.VERSION, C5's NOTEBOOK_VERSION and C2's default tag must move together (DEC-027)
        import core
        self.assertIn(f'tag = "v{core.VERSION}"', cell("C2"))
        self.assertIn(f'NOTEBOOK_VERSION = "{core.VERSION}"', cell("C5"))
        self.assertIn("notebook_version=NOTEBOOK_VERSION", cell("C5"))


class ExecuteCellsTest(unittest.TestCase):

    def exec_cell(self, source: str, namespace: dict | None = None) -> str:
        """Run a cell; pass the same namespace to share variables between cells, as Colab does."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(source, "<cell>", "exec"),
                 namespace if namespace is not None else {"__name__": "__cell__"})
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

    def test_no_drive_redirects_the_default_config(self):
        # the cell tests below rely on this; on Colab the real Drive is mounted
        with no_drive():
            root = config.resolve_paths(config.load_config()).drive_root
        self.assertEqual(root.parent.name, "not-mounted")
        self.assertFalse(root.exists())

    def test_c4_lists_inbox_or_explains_choice(self):
        with no_drive():
            self.assertIn("not found", self.exec_cell(cell("C4")))
            out = self.exec_cell(cell("C4").replace('recording = ""', 'recording = "clip.mp4"'))
        self.assertIn("'clip.mp4' is not a recording in the inbox", out)

    def test_c5_refuses_code_from_another_release(self):
        import core
        real = core.VERSION
        try:
            core.VERSION = "0.3.9"
            out = self.exec_cell(cell("C5"))
            del core.VERSION                     # code from before release numbers existed
            older = self.exec_cell(cell("C5"))
        finally:
            core.VERSION = real
        self.assertIn(f"C2 downloaded release 0.3.9. Set C2's tag to v{real}", out)
        self.assertIn("C2 downloaded release 0.3.0 or older", older)

    def test_c4_c5_explain_a_reset_runtime(self):
        # A reset runtime loses C2's code; the owner ran C5 alone and got ModuleNotFoundError
        with mock.patch.dict(sys.modules, {"core": None}):
            shared = {"__name__": "__cell__"}
            c4 = self.exec_cell(cell("C4").replace('recording = ""', 'recording = "clip.mp4"'), shared)
            c5 = self.exec_cell(cell("C5"), shared)
            alone = self.exec_cell(cell("C5"))
        for out in (c4, c5, alone):
            self.assertIn("Python can't find 'core': the Colab runtime was reset", out)
            self.assertIn("Use Runtime → Run all", out)

    def test_c5_needs_c4_then_reports_stage_error(self):
        self.assertIn("Choose a recording in cell C4 first", self.exec_cell(cell("C5")))
        shared = {"__name__": "__cell__"}
        with no_drive():
            self.exec_cell(cell("C4").replace('recording = ""', 'recording = "clip.mp4"'), shared)
            out = self.exec_cell(cell("C5"), shared)
        self.assertIn("S0 Preflight failed:", out)
        self.assertIn("Run cell C1", out)

    def test_c6_prints_the_overview(self):
        with no_drive():
            out = self.exec_cell(cell("C6"))
        self.assertIn("Work folder", out)
        self.assertIn("not found. Run cell C1", out)

    def test_c7_runs_the_tests_only_when_ticked(self):
        calls = []
        with mock.patch.object(selftest, "run", lambda: calls.append("run")):
            out = self.exec_cell(cell("C7"))
            self.assertEqual(calls, [])
            self.assertIn("Tick run_self_test", out)
            self.exec_cell(cell("C7").replace("run_self_test = False", "run_self_test = True"))
        self.assertEqual(calls, ["run"])

    def test_c6_c7_explain_a_reset_runtime(self):
        with mock.patch.dict(sys.modules, {"core": None}):
            for name in ("C6", "C7"):
                self.assertIn("the Colab runtime was reset", self.exec_cell(cell(name)))


if __name__ == "__main__":
    unittest.main()
