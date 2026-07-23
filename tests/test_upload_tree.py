"""Stdlib-only tests for the folder-upload service logic.

Loads ``upload_service`` (and its ``judge_bundler`` dependency) via importlib
with stub parent packages, so the Flask-importing ``app/__init__.py`` is never
executed. This lets the core staging/bundling/materialise logic be tested
without Flask installed.
"""

import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    for name, sub in (("app", None), ("app.runners", "app/runners"),
                      ("app.services", "app/services")):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            if sub:
                mod.__path__ = [str(ROOT / sub)]
            else:
                mod.__path__ = [str(ROOT / "app")]
            sys.modules[name] = mod
    for name, path in (
        ("app.runners.judge_bundler", "app/runners/judge_bundler.py"),
        ("app.services.upload_service", "app/services/upload_service.py"),
    ):
        spec = importlib.util.spec_from_file_location(name, ROOT / path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules["app.services.upload_service"]


upload_service = _load()


class _FakeStorage:
    """Mimics the tiny slice of Werkzeug's FileStorage we use."""

    def __init__(self, src: Path, filename: str):
        self._src = src
        self.filename = filename

    def save(self, dst: str):
        shutil.copyfile(self._src, dst)


JUDGE_SRC = """# coding: UTF-8
try:
    from synopsys.silver import *
except ImportError:
    from qtronic.silver import *
import os, sys
base = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.normpath(os.path.join(base, "../../Lib")))
from Helper import hello
print(hello())
"""


class TestUploadTree(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # A local source tree the "browser" would read.
        self.src = self.tmp / "Proj"
        tc = self.src / "01_Spec" / "TC_A"
        tc.mkdir(parents=True)
        (tc / "judge.py").write_text(JUDGE_SRC, encoding="utf-8")
        lib = self.src / "Lib"
        lib.mkdir(parents=True)
        (lib / "Helper.py").write_text("def hello():\n    return 'x'\n", encoding="utf-8")
        # A second, unselected test case.
        tc2 = self.src / "01_Spec" / "TC_B"
        tc2.mkdir(parents=True)
        (tc2 / "judge.py").write_text("print('b')\n", encoding="utf-8")

        self.model = self.tmp / "model.sil"
        self.model.write_text("silver", encoding="utf-8")
        self.ws = self.tmp / "ws"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _items(self):
        items = []
        for path in sorted(self.src.rglob("*")):
            if path.is_file():
                # webkitRelativePath includes the selected folder name "Proj".
                rel = "Proj/" + path.relative_to(self.src).as_posix()
                items.append((rel, _FakeStorage(path, rel)))
        return items

    def test_safe_relpath_strips_top(self):
        self.assertEqual(upload_service._safe_relpath("Proj/Lib/Helper.py"), "Lib/Helper.py")
        self.assertIsNone(upload_service._safe_relpath("Proj/../etc/passwd"))

    def test_stage_detects_and_bundles(self):
        info = upload_service.stage_tree(self._items(), self.ws)
        self.assertEqual(set(info["test_ids"]), {"01_Spec/TC_A", "01_Spec/TC_B"})
        run = upload_service.staged_run_dir(self.ws, info["upload_key"])
        judge = (run / "01_Spec/TC_A/judge.py").read_text(encoding="utf-8")
        # Local Helper was inlined server-side (library present in the tree).
        self.assertTrue(upload_service.is_bundled(judge))

    def test_materialise_one_is_isolated(self):
        info = upload_service.stage_tree(self._items(), self.ws)
        dest_case = Path(self.ws) / "run" / "01_Spec/TC_A"
        upload_service.materialise_one(
            self.ws, info["upload_key"], dest_case, "01_Spec/TC_A",
            self.model, "model.sil",
        )
        run = Path(self.ws) / "run"
        self.assertTrue((run / "01_Spec/TC_A/judge.py").is_file())
        # Model is copied beside the test-case folder.
        self.assertTrue((dest_case.parent / "model.sil").is_file())
        # Only the selected case + model — no other test case, no Lib folder.
        self.assertFalse((run / "01_Spec/TC_B").exists())
        self.assertFalse((run / "Lib").exists())

    def test_materialise_one_without_model(self):
        # Server-side model registry: no model is copied into the workspace.
        info = upload_service.stage_tree(self._items(), self.ws)
        dest_case = Path(self.ws) / "run" / "01_Spec/TC_A"
        upload_service.materialise_one(
            self.ws, info["upload_key"], dest_case, "01_Spec/TC_A",
        )
        self.assertTrue((dest_case / "judge.py").is_file())
        self.assertFalse((dest_case.parent / "model.sil").exists())

    def test_lib_folder_is_used_for_bundling(self):
        # A judge that imports a module living in a separately-uploaded ``lib``
        # folder must be bundled self-contained using that folder as a search
        # root. The test-case folder itself carries no library.
        src = self.tmp / "Cases"
        tc = src / "TC_LIB"
        tc.mkdir(parents=True)
        judge = (
            "# coding: UTF-8\n"
            "try:\n"
            "    from synopsys.silver import *\n"
            "except ImportError:\n"
            "    pass\n"
            "from Widget import spin\n"
            "print(spin())\n"
        )
        (tc / "judge.py").write_text(judge, encoding="utf-8")

        lib = self.tmp / "lib"
        lib.mkdir()
        (lib / "Widget.py").write_text("def spin():\n    return 42\n", encoding="utf-8")

        def items(root, top):
            out = []
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    rel = top + "/" + p.relative_to(root).as_posix()
                    out.append((rel, _FakeStorage(p, rel)))
            return out

        info = upload_service.stage_tree(
            items(src, "Cases"), self.ws,
            lib_items=items(lib, "lib"),
        )
        self.assertIn("TC_LIB", info["test_ids"])
        run = upload_service.staged_run_dir(self.ws, info["upload_key"])
        bundled = (run / "TC_LIB/judge.py").read_text(encoding="utf-8")
        self.assertTrue(upload_service.is_bundled(bundled))
        # The Widget source was embedded (base64) into the self-contained judge.
        self.assertIn("Widget", bundled)
        # The lib folder was staged under run/lib.
        self.assertTrue((run / "lib" / "Widget.py").is_file())

    def test_nested_lib_modules_are_bundled(self):
        # Mirrors a real judge: several flat ``from X import *`` where the X
        # modules live in SUB-folders of the uploaded lib/stdlib trees, plus a
        # transitive import (Constant -> MCU_Constant). All must be embedded.
        src = self.tmp / "Cases"
        tc = src / "TC_NEST"
        tc.mkdir(parents=True)
        judge = (
            "# coding: UTF-8\n"
            "try:\n"
            "    from synopsys.silver import *\n"
            "except ImportError:\n"
            "    pass\n"
            "from Common_Constant import *\n"
            "from Constant import *\n"
            "from Bit import *\n"
            "print(BASE, MCU, bit(1, 1, 6))\n"
        )
        (tc / "judge.py").write_text(judge, encoding="utf-8")

        # lib folder: modules nested under sub-directories.
        lib = self.tmp / "lib"
        (lib / "area1").mkdir(parents=True)
        (lib / "area1" / "Common_Constant.py").write_text(
            "BASE = 1\n", encoding="utf-8"
        )
        (lib / "area2").mkdir(parents=True)
        (lib / "area2" / "Bit.py").write_text(
            "def bit(pos, length, data):\n"
            "    return (data & ((2 ** length - 1) << pos)) >> pos\n",
            encoding="utf-8",
        )
        # stdlib folder: a module that transitively imports another nested one.
        std = self.tmp / "stdlib"
        (std / "sub").mkdir(parents=True)
        (std / "sub" / "Constant.py").write_text(
            "from MCU_Constant import *\n", encoding="utf-8"
        )
        (std / "sub" / "deep").mkdir(parents=True)
        (std / "sub" / "deep" / "MCU_Constant.py").write_text(
            "MCU = 9\n", encoding="utf-8"
        )

        def items(root, top):
            out = []
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    rel = top + "/" + p.relative_to(root).as_posix()
                    out.append((rel, _FakeStorage(p, rel)))
            return out

        info = upload_service.stage_tree(
            items(src, "Cases"), self.ws,
            lib_items=items(lib, "lib"),
            stdlib_items=items(std, "stdlib"),
        )
        run = upload_service.staged_run_dir(self.ws, info["upload_key"])
        bundled = (run / "TC_NEST/judge.py").read_text(encoding="utf-8")
        self.assertTrue(upload_service.is_bundled(bundled))
        for name in ("Common_Constant", "Bit", "Constant", "MCU_Constant"):
            self.assertIn(name, bundled, f"{name} not embedded")
        # No unresolved-import warnings should have been produced.
        self.assertEqual(
            [n for n in info["notes"] if "unresolved" in n], [], info["notes"]
        )

    def test_real_library_folder_layout_is_bundled(self):
        # Mirrors the real project: the tester uploads the whole
        # ``02_Config/Library`` folder (Lib/LibValue/StdLib/SystemVariable) as the
        # library upload, and the judge uses the production prelude that appends
        # ``02_Config/Library/Lib`` etc. and imports flatly.
        src = self.tmp / "Cases"
        tc = src / "01_Spec_Report" / "TC_REAL"
        tc.mkdir(parents=True)
        judge = (
            "# coding: UTF-8\n"
            "try:\n"
            "    from synopsys.silver import *\n"
            "    from synopsys.util import scheduler\n"
            "except ImportError:\n"
            "    from qtronic.silver import *\n"
            "    from qtronic.util import scheduler\n"
            "import os, sys\n"
            "base = os.path.dirname(os.path.abspath(__file__))\n"
            "Lib_in_path = os.path.normpath(os.path.join(base, '../../Lib/'))\n"
            "Lib_co_path = os.path.normpath(os.path.join("
            "base.split('01_Spec_Report')[0], './02_Config/Library/Lib/'))\n"
            "Lib_std_path = os.path.normpath(os.path.join("
            "base.split('01_Spec_Report')[0], './02_Config/Library/StdLib/'))\n"
            "sys.path.append(Lib_in_path)\n"
            "sys.path.append(Lib_co_path)\n"
            "sys.path.append(Lib_std_path)\n"
            "from Common_Constant import *\n"
            "from Constant import *\n"
            "from Bit import *\n"
            "print(BASE, MCU, bit(0, 6, 1))\n"
        )
        (tc / "judge.py").write_text(judge, encoding="utf-8")

        # The uploaded 02_Config\Library folder with its four sub-folders.
        library = self.tmp / "Library"
        (library / "Lib").mkdir(parents=True)
        (library / "Lib" / "Common_Constant.py").write_text(
            "BASE = 1\n", encoding="utf-8"
        )
        (library / "Lib" / "Bit.py").write_text(
            "def bit(pos, length, data):\n"
            "    return (data & ((2 ** length - 1) << pos)) >> pos\n",
            encoding="utf-8",
        )
        (library / "StdLib").mkdir(parents=True)
        (library / "StdLib" / "Constant.py").write_text(
            "from MCU_Constant import *\n", encoding="utf-8"
        )
        (library / "StdLib" / "MCU_Constant.py").write_text(
            "MCU = 9\n", encoding="utf-8"
        )
        (library / "LibValue").mkdir(parents=True)
        (library / "LibValue" / "Value.py").write_text("V = 3\n", encoding="utf-8")
        (library / "SystemVariable").mkdir(parents=True)
        (library / "SystemVariable" / "Sys.py").write_text("S = 4\n", encoding="utf-8")

        def items(root, top):
            out = []
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    rel = top + "/" + p.relative_to(root).as_posix()
                    out.append((rel, _FakeStorage(p, rel)))
            return out

        # The tester picks the "Library" folder in the stdlib field.
        info = upload_service.stage_tree(
            items(src, "Cases"), self.ws,
            stdlib_items=items(library, "Library"),
        )
        run = upload_service.staged_run_dir(self.ws, info["upload_key"])
        bundled = (run / "01_Spec_Report/TC_REAL/judge.py").read_text(encoding="utf-8")
        self.assertTrue(upload_service.is_bundled(bundled))
        for name in ("Common_Constant", "Bit", "Constant", "MCU_Constant"):
            self.assertIn(name, bundled, f"{name} not embedded")
        self.assertEqual(
            [n for n in info["notes"] if "unresolved" in n], [], info["notes"]
        )

    def test_missing_lib_module_is_reported(self):
        # A judge importing a helper whose folder was NOT uploaded should still
        # bundle (best effort) but surface a clear "not found" warning note.
        src = self.tmp / "Cases"
        tc = src / "TC_MISS"
        tc.mkdir(parents=True)
        (tc / "judge.py").write_text(
            "# coding: UTF-8\n"
            "try:\n    from synopsys.silver import *\nexcept ImportError:\n    pass\n"
            "from Nowhere import gone\n"
            "print(gone)\n",
            encoding="utf-8",
        )

        def items(root, top):
            out = []
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    rel = top + "/" + p.relative_to(root).as_posix()
                    out.append((rel, _FakeStorage(p, rel)))
            return out

        info = upload_service.stage_tree(items(src, "Cases"), self.ws)
        self.assertTrue(
            any("Nowhere" in n and "unresolved" in n for n in info["notes"]),
            info["notes"],
        )


if __name__ == "__main__":
    unittest.main()
