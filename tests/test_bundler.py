"""Tests for the self-contained judge bundler (pure stdlib; no Flask needed)."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

# Load the bundler module directly from its file so this test runs even without
# Flask installed (importing the ``app`` package would pull in Flask).
_BUNDLER_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "runners" / "judge_bundler.py"
)
_spec = importlib.util.spec_from_file_location("judge_bundler", _BUNDLER_PATH)
judge_bundler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(judge_bundler)


JUDGE_HEADER = '''\
# coding: UTF-8
try:
    from synopsys.silver import *
except ImportError:
    from qtronic.silver import *
import os, sys
base = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.normpath(os.path.join(base, "../Lib")))
from Common_Constant import *
from Bit import bit_set
'''

JUDGE_BODY = '''\
def run():
    return bit_set(BASE_VALUE)
'''


def _make_project(root: Path) -> Path:
    tc = root / "TC_DEMO"
    tc.mkdir(parents=True)
    lib = root / "Lib"
    lib.mkdir(parents=True)
    (lib / "Common_Constant.py").write_text("BASE_VALUE = 7\n", encoding="utf-8")
    (lib / "Bit.py").write_text(
        "from Common_Constant import BASE_VALUE\n"
        "def bit_set(x):\n    return x | BASE_VALUE\n",
        encoding="utf-8",
    )
    judge = tc / "judge.py"
    judge.write_text(JUDGE_HEADER + JUDGE_BODY, encoding="utf-8")
    return judge


class TestJudgeBundler(unittest.TestCase):
    def test_discovers_lib_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            judge = _make_project(Path(tmp))
            dirs = judge_bundler.discover_search_paths(judge)
            self.assertTrue(any(d.name == "Lib" for d in dirs), dirs)

    def test_bundles_local_modules(self) -> None:
        with TemporaryDirectory() as tmp:
            judge = _make_project(Path(tmp))
            bundled = judge_bundler.bundle_judge(judge)
            self.assertIn("_BUNDLED_MODULES", bundled)
            self.assertIn("Common_Constant", bundled)
            self.assertIn("Bit", bundled)
            # Silver + stdlib imports remain untouched (not inlined).
            self.assertIn("from synopsys.silver import *", bundled)

    def test_bundled_script_runs_without_lib_dirs(self) -> None:
        with TemporaryDirectory() as tmp:
            judge = _make_project(Path(tmp))
            bundled = judge_bundler.bundle_judge(judge)

        # Stub the Silver package so ``from synopsys.silver import *`` works.
        silver = types.ModuleType("synopsys")
        silver.__path__ = []
        sub = types.ModuleType("synopsys.silver")
        sub.__all__ = []
        saved = {k: sys.modules.get(k) for k in ("synopsys", "synopsys.silver")}
        sys.modules["synopsys"] = silver
        sys.modules["synopsys.silver"] = sub
        try:
            ns: dict = {"__name__": "judge_under_test", "__file__": "judge.py"}
            exec(compile(bundled, "<bundled>", "exec"), ns)  # noqa: S102
            self.assertEqual(ns["run"](), 7 | 7)
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_bundled_module_can_use_dunder_file(self) -> None:
        # A library module that references ``__file__`` at import time (very
        # common: ``os.path.dirname(os.path.abspath(__file__))``) must not raise
        # ``NameError`` once embedded -- the loader assigns each module a
        # ``__file__`` in the judge's directory.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tc = root / "TC"
            tc.mkdir()
            lib = root / "Lib"
            lib.mkdir()
            (lib / "Extend.py").write_text(
                "import os\n"
                "HERE = os.path.dirname(os.path.abspath(__file__))\n"
                "def where():\n    return HERE\n",
                encoding="utf-8",
            )
            judge = tc / "judge.py"
            judge.write_text(
                "# coding: UTF-8\n"
                "import os, sys\n"
                "base = os.path.dirname(os.path.abspath(__file__))\n"
                "sys.path.append(os.path.normpath(os.path.join(base, '../Lib')))\n"
                "from Extend import where\n"
                "def run():\n    return where()\n",
                encoding="utf-8",
            )
            bundled = judge_bundler.bundle_judge(judge)
            self.assertIn("Extend", bundled)

        ns: dict = {"__name__": "judge_under_test", "__file__": str(judge)}
        exec(compile(bundled, "<bundled>", "exec"), ns)  # noqa: S102
        # No NameError; ``__file__`` resolved to the judge's directory.
        self.assertEqual(ns["run"](), str(Path(str(judge)).resolve().parent))

    def test_current_bundle_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            judge = _make_project(Path(tmp))
            first = judge_bundler.bundle_judge(judge)
            judge.write_text(first, encoding="utf-8")
            second = judge_bundler.bundle_judge(judge)
            self.assertEqual(first, second)  # up-to-date bundle left untouched
            self.assertTrue(judge_bundler.is_current_bundle(first))

    def test_stale_bundle_is_rebundled(self) -> None:
        # A judge bundled by an OLDER bootstrap (e.g. one missing the __file__
        # fix) must be re-processed on re-upload, not skipped as "already done".
        with TemporaryDirectory() as tmp:
            judge = _make_project(Path(tmp))
            fresh = judge_bundler.bundle_judge(judge)
            stale = fresh.replace(" (v2).", ".").replace(
                "_JUDGE_DIR = _os.path.dirname(_JUDGE_FILE)", "")
            self.assertTrue(judge_bundler.is_bundled(stale))
            self.assertFalse(judge_bundler.is_current_bundle(stale))
            judge.write_text(stale, encoding="utf-8")

            rebundled = judge_bundler.bundle_judge(judge)
            self.assertTrue(judge_bundler.is_current_bundle(rebundled))
            self.assertIn("_JUDGE_DIR", rebundled)  # the fix is present again
            self.assertEqual(rebundled, fresh)      # equivalent to a fresh bundle

    def test_best_effort_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            judge = Path(tmp) / "judge.py"
            judge.write_text("import os\nX = 1\n", encoding="utf-8")
            source, error = judge_bundler.bundle_judge_or_original(judge)
            self.assertIsNone(error)
            self.assertIn("X = 1", source)

    def test_bundles_judge_with_utf8_bom(self) -> None:
        # A judge.py saved with a UTF-8 BOM (e.g. by Notepad) must still parse
        # and bundle; the leading U+FEFF previously broke ``ast.parse``.
        with TemporaryDirectory() as tmp:
            judge = _make_project(Path(tmp))
            judge.write_text(
                JUDGE_HEADER + JUDGE_BODY, encoding="utf-8-sig"
            )  # writes a leading BOM
            raw = judge.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "BOM not written")

            dirs = judge_bundler.discover_search_paths(judge)
            self.assertTrue(any(d.name == "Lib" for d in dirs), dirs)

            bundled = judge_bundler.bundle_judge(judge)
            self.assertIn("_BUNDLED_MODULES", bundled)
            self.assertIn("Common_Constant", bundled)
            self.assertFalse(bundled.startswith("\ufeff"))

            source, error = judge_bundler.bundle_judge_or_original(judge)
            self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
