"""Vendored JSON test-runner framework (from the ``testcase_json`` project).

These three modules are copied verbatim (plus a small ``sys.path`` guard in the
runner) so a Silver run can execute a data-driven test case straight from JSON
inputs instead of the legacy ``python judge.py`` flow:

* ``silver_json_runner.py`` — the script Silver loads (``Python.dll`` entry
  points ``pre_init`` / ``MainGenerator`` / ``pre_cleanup``). It reads the test
  case, ``constants.json`` and ``lib.json`` sitting next to it.
* ``silver_test_framework.py`` — the reusable execution engine.
* ``framework_builtins.py`` — the embedded ``Wait`` / ``bit`` / ``Extend`` /
  ``MagicList`` builtins.

``materialise_run_dir`` (see ``services.lanmatrix.silver_json_export``) copies
these three files, together with the generated JSON documents, into a task's
``run/<test_id>/`` directory. ``silver_runner`` detects the presence of
``silver_json_runner.py`` there and invokes it in place of ``judge.py``.
"""
from __future__ import annotations

import shutil
from pathlib import Path

FRAMEWORK_DIR = Path(__file__).resolve().parent

RUNNER_NAME = "silver_json_runner.py"

FRAMEWORK_FILES: tuple[str, ...] = (
    "silver_json_runner.py",
    "silver_test_framework.py",
    "framework_builtins.py",
)


def copy_framework(dst_dir: Path) -> Path:
    """Copy the three framework files into *dst_dir* (created if needed).

    Returns the path of the copied ``silver_json_runner.py``.
    """
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for name in FRAMEWORK_FILES:
        shutil.copy2(FRAMEWORK_DIR / name, dst_dir / name)
    return dst_dir / RUNNER_NAME
