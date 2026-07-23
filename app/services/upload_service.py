"""Upload handling: stage uploaded files, inspect them, materialise workspaces.

The platform is a pure web app. In the browser the user selects a test-case
**folder** (``<input webkitdirectory>``); the browser uploads that whole tree,
including the sibling ``Lib`` / ``LibValue`` library folders the judges rely on.
Because the libraries are present on the server at this point,
:func:`_bundle_all_judges` inlines them into each ``judge.py`` server-side,
producing self-contained judges. The bundled judge no longer needs the library
folders at run time, so each task workspace only keeps its own test-case folder.

The ``.sil`` plant model is **not** uploaded here -- it is a shared asset the
administrator configures once (see :mod:`.model_service`). Each task is run
against that admin-configured model, copied into the task workspace at
materialise time.

Two entry points:

* :func:`stage_tree` -- save an uploaded directory tree, bundle its judges, and
  report the detected test ids (folders containing a ``judge.py``).
* :func:`materialise_one` -- copy a single selected test-case folder plus the
  admin model into a fresh task workspace.
"""

from __future__ import annotations

import posixpath
import shutil
import uuid
from pathlib import Path
from typing import List, Optional

from ..runners.judge_bundler import (
    bundle_judge_or_original,
    is_bundled,
    unresolved_local_imports,
)


class UploadError(Exception):
    """Raised for invalid or unsafe uploads."""


def _staging_root(workspace_dir: Path) -> Path:
    return Path(workspace_dir) / "staging"


# Sub-directory names (under the staging run dir) that hold the uploaded
# ``lib`` / ``stdlib`` folders. They are added as extra import-search roots for
# the judge bundler so a judge's ``import <module>`` / ``from <module> import *``
# resolves against them and gets inlined ("replaced") into a self-contained
# script.
LIB_DIRNAME = "lib"
STDLIB_DIRNAME = "stdlib"


# Directory names never used as import-search roots (test-case artefacts, VCS,
# Python caches). Everything else that holds a ``.py`` file is a candidate root.
_SEARCH_SKIP_DIRS = {"__pycache__", ".git", ".svn", ".hg", ".idea", ".vscode"}


def _iter_module_dirs(root: Path) -> List[Path]:
    """Every directory at/under *root* that directly contains a ``.py`` module.

    The uploaded ``lib`` / ``stdlib`` folders frequently nest their modules in
    sub-folders (e.g. ``StdLib/<area>/Foo.py``). A judge's flat
    ``from Foo import *`` only resolves if *that* sub-folder is a search root, so
    every module-bearing directory is collected -- not just the top level.
    """
    if not root.is_dir():
        return []
    found: List[Path] = []
    for dirpath, dirnames, filenames in _walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SEARCH_SKIP_DIRS]
        if any(f.endswith(".py") for f in filenames):
            found.append(Path(dirpath).resolve())
    return found


def _walk(root: Path):
    import os

    return os.walk(root)


def _bundle_search_dirs(run_dir: Path) -> List[Path]:
    """Extra bundler search roots derived from the uploaded lib/stdlib folders.

    Includes every module-bearing directory (recursively) under the uploaded
    ``lib`` / ``stdlib`` folders -- so a judge's flat ``from <module> import *``
    resolves no matter how deeply the module is nested -- plus the run-dir root
    (so package-style ``import lib.<module>`` resolves).
    """
    dirs: List[Path] = []
    seen = set()

    def _add(p: Path) -> None:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            dirs.append(rp)

    has_lib = False
    for name in (LIB_DIRNAME, STDLIB_DIRNAME):
        candidate = run_dir / name
        if candidate.is_dir():
            has_lib = True
            for module_dir in _iter_module_dirs(candidate):
                _add(module_dir)
    if has_lib:
        _add(run_dir)
    return dirs


def _bundle_all_judges(run_dir: Path,
                       extra_search_dirs: Optional[List[Path]] = None) -> List[str]:
    """Best-effort, idempotent judge bundling for browser uploads.

    Local modules are inlined ("replaced") using both the paths the judge appends
    to ``sys.path`` and the uploaded ``lib`` / ``stdlib`` folders
    (``extra_search_dirs``). Judges already bundled by the CURRENT version are
    left untouched; those bundled by an OLDER version are re-bundled (handled
    inside :func:`bundle_judge`) so bootstrap bug-fixes reach them.
    """
    extras = list(extra_search_dirs or [])
    notes: List[str] = []
    for judge in sorted(run_dir.rglob("judge.py")):
        if not judge.is_file():
            continue
        missing = unresolved_local_imports(judge, extras)
        source, error = bundle_judge_or_original(judge, extras)
        judge.write_text(source, encoding="utf-8")
        if error:
            notes.append(f"{judge.parent.name}: bundling fell back ({error})")
        if missing:
            notes.append(
                f"{judge.parent.name}: modules not found in the uploaded "
                f"lib/stdlib folders and left unresolved -- "
                f"{', '.join(missing)}. Upload the folder(s) containing them."
            )
    return notes


def detect_test_ids(run_dir: Path) -> List[str]:
    """Directories (any depth) that directly contain a ``judge.py``.

    The uploaded ``lib`` / ``stdlib`` helper folders are never treated as test
    cases even if they happen to contain a ``judge.py``.
    """
    ids = set()
    for judge in run_dir.rglob("judge.py"):
        rel = judge.parent.relative_to(run_dir).as_posix()
        top = rel.split("/", 1)[0]
        if top in (LIB_DIRNAME, STDLIB_DIRNAME):
            continue
        ids.add(rel if rel != "." else judge.parent.name)
    return sorted(ids)


def _safe_relpath(raw: str) -> Optional[str]:
    """Normalise an uploaded relative path, rejecting unsafe ones.

    Strips the leading directory segment the browser prepends (the name of the
    folder the user selected) so paths are relative to the chosen root, and
    guards against absolute paths / ``..`` traversal.
    """
    if not raw:
        return None
    rel = raw.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    if len(parts) <= 1:  # just the top folder or a bare file name
        return posixpath.join(*parts) if parts else None
    return posixpath.join(*parts[1:])  # drop the selected-folder segment


def _save_items(items, dest_dir: Path, run_root: Path) -> int:
    """Save ``(relative_path, FileStorage)`` pairs under *dest_dir* safely."""
    run_resolved = run_root.resolve()
    count = 0
    for raw_path, storage in items or []:
        rel = _safe_relpath(raw_path or getattr(storage, "filename", ""))
        if not rel:
            continue
        target = (dest_dir / rel).resolve()
        if not str(target).startswith(str(run_resolved)):
            raise UploadError(f"Unsafe path in upload: {raw_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        storage.save(str(target))
        count += 1
    return count


def stage_tree(file_items, workspace_dir: Path,
               lib_items=None, stdlib_items=None) -> dict:
    """Stage an uploaded directory tree and report its test ids.

    ``file_items`` is an iterable of ``(relative_path, FileStorage)`` pairs
    (typically ``zip(request.form.getlist("paths"), request.files.getlist("files"))``
    or the FileStorage ``filename`` used as the path) for the test-case folder.

    ``lib_items`` / ``stdlib_items`` are the optional contents of the tester's
    ``lib`` and ``stdlib`` folders. They are written under ``run/lib`` and
    ``run/stdlib`` and used as extra import-search roots so each ``judge.py`` is
    rewritten into a self-contained script (its local imports "replaced"/inlined
    from those folders).
    """
    upload_key = uuid.uuid4().hex
    run_dir = _staging_root(workspace_dir) / upload_key / "run"
    run_dir.mkdir(parents=True, exist_ok=True)

    count = _save_items(file_items, run_dir, run_dir)
    _save_items(lib_items, run_dir / LIB_DIRNAME, run_dir)
    _save_items(stdlib_items, run_dir / STDLIB_DIRNAME, run_dir)

    if count == 0:
        cleanup_staging(workspace_dir, upload_key)
        raise UploadError("No files were received from the selected folder.")

    notes = _bundle_all_judges(run_dir, _bundle_search_dirs(run_dir))
    test_ids = detect_test_ids(run_dir)
    if not test_ids:
        cleanup_staging(workspace_dir, upload_key)
        raise UploadError(
            "No judge.py found in the selected folder. A test case is a folder "
            "that contains a judge.py."
        )
    return {"upload_key": upload_key, "test_ids": test_ids, "notes": notes}


def materialise_one(
    workspace_dir: Path,
    upload_key: str,
    dest_case: Path,
    test_id: str,
    model_src: Optional[Path] = None,
    model_name: Optional[str] = None,
) -> Path:
    """Assemble the run-script staging folder for a single test id.

    Copies only that test-case folder (its judge is already self-contained) into
    *dest_case* (the ``<test id>`` scripts folder the runner expects). The
    ``.sil`` model is **not** copied by default: with the server-side model
    registry the runner opens the admin-registered path in place. For the legacy
    in-bundle flow a ``model_src`` / ``model_name`` may still be supplied to copy
    the model beside the test-case folder.
    """
    src_run = staged_run_dir(workspace_dir, upload_key)
    src_case = src_run / test_id
    if not (src_case / "judge.py").is_file():
        raise UploadError(f"Test case '{test_id}' not found in the upload.")

    dest_case = Path(dest_case)
    dest_case.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_case, dest_case, dirs_exist_ok=True)

    if model_src is not None and model_name:
        model_dst = dest_case.parent / model_name
        model_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(model_src, model_dst)
    return dest_case


def staged_run_dir(workspace_dir: Path, upload_key: str) -> Path:
    return _staging_root(workspace_dir) / upload_key / "run"


def cleanup_staging(workspace_dir: Path, upload_key: str) -> None:
    staging = _staging_root(workspace_dir) / upload_key
    shutil.rmtree(staging, ignore_errors=True)
