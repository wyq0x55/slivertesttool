"""Result packaging + download resolution."""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ..models import Task, TaskStatus

# Artefacts that must never be folded into an on-demand report archive.
_SKIP_NAMES = {"report.zip"}


def result_dir(task: Task, run_count=None) -> Optional[Path]:
    """The on-disk results directory for a task (its per-test ``log`` dir).

    Returns ``None`` when the workspace is unknown or the directory is absent.
    This is the source we compress from on demand, so no ``report.zip`` snapshot
    needs to be stored ahead of time.
    """
    if not task.workspace:
        return None
    from . import run_evidence_service as evidence
    try:
        root = evidence.attempt_dir(task, run_count)
        if root.parent.exists() or run_count is not None or ".evidence" in Path(task.report_path or "").parts:
            data = evidence.read_evidence(task, run_count, max_log_bytes=0)
            if data["outcome"] is None:
                return None
            return evidence.checked_path(root, root / "results", exists=True)
    except (ValueError, OSError):
        return None
    from ..runners import run_layout
    path = run_layout.log_dir(task.workspace, task.test_id)
    if not path.is_dir():
        return None
    try:
        expected_parent = Path(task.workspace).resolve() / "log"
        if path.resolve(strict=True).parent != expected_parent:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return path


def _iter_result_files(log_dir: Path) -> Iterator[Path]:
    from .run_evidence_service import checked_path
    resolved_root = log_dir.resolve()
    for path in sorted(log_dir.rglob("*")):
        if path.name in _SKIP_NAMES or path.is_symlink() or not path.is_file():
            continue
        try:
            checked_path(log_dir, path, exists=True)
            path.resolve(strict=True).relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            continue
        yield path


def has_result(task: Task, run_count=None) -> bool:
    """Whether any downloadable artefact exists for the task."""
    log_dir = result_dir(task, run_count)
    if log_dir is None:
        return False
    return any(_iter_result_files(log_dir))


def _add_result_files(zf: zipfile.ZipFile, log_dir: Path, arc_root: str) -> int:
    added = 0
    for path in _iter_result_files(log_dir):
        rel = path.relative_to(log_dir)
        arcname = str(Path(arc_root) / rel) if arc_root else str(rel)
        zf.write(path, arcname)
        added += 1
    return added


def build_report_stream(task: Task, run_count=None) -> Optional[io.BytesIO]:
    """Compress a task's results on demand into an in-memory zip buffer.

    Returns ``None`` when the task has no artefacts to offer.
    """
    buffer, _ = build_reports_stream([task], run_count=run_count)
    return buffer


def build_reports_stream(tasks: Iterable[Task], run_count=None) -> tuple[Optional[io.BytesIO], int]:
    """Bundle selected results under ``log/`` with a relative-path CSV index.

    Keep raw test IDs in the index and preserve filesystem-safe punctuation
    (including Silver's ``ID;;`` prefix) in export folders. Runtime directory
    names remain independent of this download layout.
    """
    from ..runners.run_layout import sanitize_segment
    from . import run_evidence_service as evidence

    buffer = io.BytesIO()
    summary = io.StringIO(newline="")
    writer = csv.writer(summary)
    writer.writerow(["Test_No.", "Result", "Path"])
    used_folders: set[str] = set()
    added = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for task in tasks:
            base = sanitize_segment(task.test_id, "testcase")
            folder = base
            suffix = 0
            while folder.casefold() in used_folders:
                suffix += 1
                folder = f"{base}_{suffix}"
            used_folders.add(folder.casefold())
            arc_root = f"log/{folder}"
            path = "-"
            verdict = (getattr(task, "result", "") or "-") if run_count is None else "-"
            log_dir = result_dir(task, run_count)
            if log_dir is not None:
                root = evidence.attempt_dir(task, run_count)
                if root.parent.exists():
                    data = evidence.read_evidence(task, run_count, max_log_bytes=0)
                    verdict = data["outcome"]["verdict"] or "-"
                if _add_result_files(bundle, log_dir, arc_root):
                    added += 1
                    path = arc_root
            writer.writerow([task.test_id, verdict, path])
        bundle.writestr("all.csv", summary.getvalue().encode("utf-8-sig"))
    if added == 0:
        return None, 0
    buffer.seek(0)
    return buffer, added


def jdgrslt_path(task: Task, run_count=None) -> Optional[Path]:
    """Locate the judge result log (``jdgrslt.log``) for a task, if present.

    It is written into the per-test log directory
    ``<workspace>/log/<test_id>/jdgrslt.log`` during execution.
    """
    log_dir = result_dir(task, run_count)
    if log_dir is None:
        return None
    path = log_dir / "jdgrslt.log"
    from .run_evidence_service import checked_path
    try:
        checked_path(log_dir, path, exists=True)
        return path if path.is_file() else None
    except (ValueError, OSError):
        return None


def report_path(task: Task, run_count=None) -> Optional[Path]:
    """Return the results directory to offer for download, if any.

    Reports are offered for both passed and failed runs, since the logs are
    useful either way. Historically this returned a pre-built ``report.zip``;
    results are now compressed on demand, so this resolves to the results dir.
    """
    return result_dir(task, run_count) if has_result(task, run_count) else None
