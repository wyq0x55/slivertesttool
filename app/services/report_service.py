"""Result packaging + download resolution."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Iterator, Optional

from ..models import Task, TaskStatus

# Artefacts that must never be folded into an on-demand report archive.
_SKIP_NAMES = {"report.zip"}


def package_logs(log_dir: Path, dest_zip: Path, arc_root: str = "") -> Path:
    """Zip every artefact under ``log_dir`` into ``dest_zip``."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    dest_zip = Path(dest_zip)
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_zip.resolve()
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(log_dir.rglob("*")):
            if path.is_file():
                # Never fold the report archive into itself when it is written
                # inside ``log_dir``.
                if path.resolve() == dest_resolved:
                    continue
                rel = path.relative_to(log_dir)
                arcname = str(Path(arc_root) / rel) if arc_root else str(rel)
                zf.write(path, arcname)
    return dest_zip


def result_dir(task: Task) -> Optional[Path]:
    """The on-disk results directory for a task (its per-test ``log`` dir).

    Returns ``None`` when the workspace is unknown or the directory is absent.
    This is the source we compress from on demand, so no ``report.zip`` snapshot
    needs to be stored ahead of time.
    """
    if not task.workspace:
        return None
    from ..runners import run_layout
    path = run_layout.log_dir(task.workspace, task.test_id)
    return path if path.is_dir() else None


def _iter_result_files(log_dir: Path) -> Iterator[Path]:
    for path in sorted(log_dir.rglob("*")):
        if path.is_file() and path.name not in _SKIP_NAMES:
            yield path


def has_result(task: Task) -> bool:
    """Whether any downloadable artefact exists for the task."""
    log_dir = result_dir(task)
    if log_dir is None:
        return False
    return any(_iter_result_files(log_dir))


def add_result_to_zip(zf: zipfile.ZipFile, task: Task, arc_root: str = "") -> int:
    """Write a task's result files into an open ``ZipFile`` under ``arc_root``.

    Returns the number of files added. Used both for single-task downloads and
    to place each task's artefacts under its own folder in a batch bundle,
    avoiding any zip-in-zip nesting.
    """
    log_dir = result_dir(task)
    if log_dir is None:
        return 0
    added = 0
    for path in _iter_result_files(log_dir):
        rel = path.relative_to(log_dir)
        arcname = str(Path(arc_root) / rel) if arc_root else str(rel)
        zf.write(path, arcname)
        added += 1
    return added


def build_report_stream(task: Task) -> Optional[io.BytesIO]:
    """Compress a task's results on demand into an in-memory zip buffer.

    Returns ``None`` when the task has no artefacts to offer.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        added = add_result_to_zip(zf, task, arc_root=task.test_id)
    if added == 0:
        return None
    buffer.seek(0)
    return buffer


def jdgrslt_path(task: Task) -> Optional[Path]:
    """Locate the judge result log (``jdgrslt.log``) for a task, if present.

    It is written into the per-test log directory
    ``<workspace>/log/<test_id>/jdgrslt.log`` during execution.
    """
    if not task.workspace:
        return None
    from ..runners import run_layout
    path = run_layout.log_dir(task.workspace, task.test_id) / "jdgrslt.log"
    return path if path.is_file() else None


def report_path(task: Task) -> Optional[Path]:
    """Return the results directory to offer for download, if any.

    Reports are offered for both passed and failed runs, since the logs are
    useful either way. Historically this returned a pre-built ``report.zip``;
    results are now compressed on demand, so this resolves to the results dir.
    """
    return result_dir(task) if has_result(task) else None
