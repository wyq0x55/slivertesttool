"""Task lifecycle service: create, query, cancel."""

from __future__ import annotations

import datetime as _dt
from typing import List, Optional

from ..extensions import db
from ..models import Task, TaskStatus


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def next_task_key(task_id: int) -> str:
    return f"T{task_id:06d}"


def get_by_key(task_key: str) -> Optional[Task]:
    return Task.query.filter_by(task_key=task_key).first()


def get_project_task(project_id: int, task_key: str) -> Optional[Task]:
    """Fetch a task by key only if it belongs to ``project_id``."""
    return Task.query.filter_by(task_key=task_key, project_id=project_id).first()


def list_tasks(limit: int = 200, submitter: Optional[str] = None,
               project_id: Optional[int] = None) -> List[Task]:
    query = Task.query
    if submitter:
        query = query.filter_by(submitter=submitter)
    if project_id is not None:
        query = query.filter_by(project_id=project_id)
    return query.order_by(Task.id.desc()).limit(limit).all()


def find_active_duplicate(submitter: str, test_id: str,
                          project_id: Optional[int] = None) -> Optional[Task]:
    """Return the submitter's queued/running task for ``test_id`` if any.

    Guards against double-clicks re-enqueuing the same test. When ``project_id``
    is given the check is scoped to that project so the same test id can run
    independently in different projects.
    """
    query = Task.query.filter(
        Task.submitter == submitter,
        Task.test_id == test_id,
        Task.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]),
    )
    if project_id is not None:
        query = query.filter(Task.project_id == project_id)
    return query.order_by(Task.id.desc()).first()


def create_task(
    task_name: str,
    file_name: str,
    submitter: str,
    test_id: str,
    sil_relpath: str,
    workspace: str,
    sil_name: str = "",
    project_id: Optional[int] = None,
    submitter_id: Optional[int] = None,
) -> Task:
    """Persist a new QUEUED task and assign its public key."""
    task = Task(
        task_key="",
        task_name=task_name or test_id,
        file_name=file_name,
        submitter=submitter or "anonymous",
        test_id=test_id,
        sil_relpath=sil_relpath,
        sil_name=sil_name,
        status=TaskStatus.QUEUED.value,
        message="Queued, waiting for a free license slot.",
        workspace=workspace,
        project_id=project_id,
        submitter_id=submitter_id,
    )
    db.session.add(task)
    db.session.flush()  # obtain the autoincrement id
    task.task_key = next_task_key(task.id)
    db.session.commit()
    return task


def find_task_by_test_id(test_id: str, project_id: Optional[int] = None) -> Optional[Task]:
    """Return the latest task for ``(project_id, test_id)`` regardless of state.

    ``test_id`` is the unique identifier of a test case *within a project*, so
    this is the key used by :func:`upsert_task` to overwrite a prior run.
    """
    query = Task.query.filter(Task.test_id == test_id)
    if project_id is not None:
        query = query.filter(Task.project_id == project_id)
    return query.order_by(Task.id.desc()).first()


def upsert_task(
    task_name: str,
    file_name: str,
    submitter: str,
    test_id: str,
    sil_relpath: str,
    workspace: str,
    sil_name: str = "",
    project_id: Optional[int] = None,
    submitter_id: Optional[int] = None,
) -> Task:
    """Create or re-queue the task for ``(project_id, test_id)``.

    ``test_id`` is unique per project: re-enqueuing an existing test id reuses
    its task row and **overwrites the stored result** (status back to QUEUED,
    result/report/timings cleared), rather than accumulating duplicates. A
    task that is currently queued/running is returned untouched so a live run is
    not clobbered mid-flight. When no task exists yet a fresh one is created.
    """
    existing = find_task_by_test_id(test_id, project_id=project_id)
    if existing is None:
        return create_task(
            task_name=task_name, file_name=file_name, submitter=submitter,
            test_id=test_id, sil_relpath=sil_relpath, sil_name=sil_name,
            workspace=workspace, project_id=project_id, submitter_id=submitter_id)

    if TaskStatus(existing.status) in (TaskStatus.QUEUED, TaskStatus.RUNNING):
        return existing

    existing.task_name = task_name or test_id
    existing.file_name = file_name
    existing.submitter = submitter or existing.submitter
    existing.submitter_id = submitter_id if submitter_id is not None else existing.submitter_id
    existing.sil_relpath = sil_relpath
    existing.sil_name = sil_name
    existing.workspace = workspace
    existing.status = TaskStatus.QUEUED.value
    existing.progress = 0
    existing.result = ""
    existing.report_path = ""
    existing.message = "Queued, waiting for a free license slot."
    existing.cancel_requested = False
    existing.created_at = _utcnow()
    existing.started_at = None
    existing.finished_at = None
    db.session.commit()
    return existing


def request_cancel(task: Task) -> str:
    """Flag a task for cancellation. Returns a short result code.

    Queued tasks are cancelled immediately; running tasks get the flag set and
    are stopped cooperatively by the worker.
    """
    status = TaskStatus(task.status)
    if status.is_final:
        return "already_final"
    task.cancel_requested = True
    if status == TaskStatus.QUEUED:
        task.status = TaskStatus.CANCELLED.value
        task.finished_at = _utcnow()
        task.message = "Cancelled before execution."
        db.session.commit()
        return "cancelled_queued"
    task.message = "Cancellation requested; stopping..."
    db.session.commit()
    return "cancelling_running"


def delete_task(task: Task) -> None:
    db.session.delete(task)
    db.session.commit()
