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


def live(query):
    """Restrict a task query to rows that are not in the recycle bin.

    Kept as one helper so a new query path cannot quietly resurrect deleted
    tasks into a list, a count or a duplicate check.
    """
    return query.filter(Task.deleted_at.is_(None))


def get_by_key(task_key: str, *, include_deleted: bool = False) -> Optional[Task]:
    q = Task.query.filter_by(task_key=task_key)
    if not include_deleted:
        q = live(q)
    return q.first()


def get_project_task(project_id: int, task_key: str,
                     *, include_deleted: bool = False) -> Optional[Task]:
    """Fetch a task by key only if it belongs to ``project_id``."""
    q = Task.query.filter_by(task_key=task_key, project_id=project_id)
    if not include_deleted:
        q = live(q)
    return q.first()


DEFAULT_LIST_LIMIT = 200
# Hard ceiling on one response. The callers used to pass 1000 and silently drop
# everything beyond it; the number matters less than the fact that the caller
# now learns it was clipped (see ``count_tasks``).
MAX_LIST_LIMIT = 2000


def clamp_limit(raw, default: int = DEFAULT_LIST_LIMIT) -> int:
    """Coerce a caller-supplied limit into [1, MAX_LIST_LIMIT].

    Anything unparseable -- or non-positive -- falls back to ``default`` rather
    than raising or clamping up to 1: a bad ``?limit=`` in a URL someone pasted
    should not 500 the task list, and honouring ``limit=0`` literally would
    show an empty list beside a non-zero total, which reads as data loss.
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return default
    return min(n, MAX_LIST_LIMIT)


def _scoped_query(submitter: Optional[str] = None,
                  project_id: Optional[int] = None):
    """Shared filter chain so ``list_tasks`` and ``count_tasks`` cannot drift.

    If these two ever disagreed, the UI would show "共 N 条" next to a list that
    was filtered differently -- a number the user has no way to reconcile.
    """
    query = live(Task.query)
    if submitter:
        query = query.filter_by(submitter=submitter)
    if project_id is not None:
        query = query.filter_by(project_id=project_id)
    return query


def list_tasks(limit: int = DEFAULT_LIST_LIMIT, submitter: Optional[str] = None,
               project_id: Optional[int] = None) -> List[Task]:
    return (_scoped_query(submitter, project_id)
            .order_by(Task.id.desc()).limit(limit).all())


def count_tasks(submitter: Optional[str] = None,
                project_id: Optional[int] = None) -> int:
    """Total matching rows, ignoring any limit.

    Needed so the UI can say "showing 200 of 4,317" instead of implying that
    200 is all there is. Truncation the user cannot see is indistinguishable
    from data loss.
    """
    return _scoped_query(submitter, project_id).count()


def find_active_duplicate(submitter: str, test_id: str,
                          project_id: Optional[int] = None) -> Optional[Task]:
    """Return the submitter's queued/running task for ``test_id`` if any.

    Guards against double-clicks re-enqueuing the same test. When ``project_id``
    is given the check is scoped to that project so the same test id can run
    independently in different projects.
    """
    query = live(Task.query).filter(
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
    # Deleted tasks are excluded so ``upsert_task`` starts a clean run instead of
    # reviving a row the user had put in the recycle bin.
    query = live(Task.query).filter(Task.test_id == test_id)
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


def cancel_project_queue(
    project_id: Optional[int],
    exclude_task_id: Optional[int] = None,
    reason: str = "Cancelled: an environment error occurred earlier in this project's queue.",
) -> List[Task]:
    """Cancel every still-QUEUED task of ``project_id`` (except ``exclude_task_id``).

    Used when one task in a project fails with an *environment* error: the shared
    setup is presumed broken, so the remaining queued tests would only pile up
    more failures. Cancelling them fast frees the licenses for other projects.

    Scope guarantees:
      * Only tasks with the SAME ``project_id`` are touched -- other projects run
        unaffected.
      * Unscoped tasks (``project_id is None``) never trigger a cascade.
      * Only QUEUED tasks are cancelled; RUNNING/finished tasks are left alone.

    Returns the list of tasks that were transitioned to CANCELLED so the caller
    can emit UI/SSE events for each.
    """
    if project_id is None:
        return []
    query = live(Task.query).filter(
        Task.project_id == project_id,
        Task.status == TaskStatus.QUEUED.value,
    )
    if exclude_task_id is not None:
        query = query.filter(Task.id != exclude_task_id)

    cancelled: List[Task] = []
    now = _utcnow()
    for task in query.all():
        task.cancel_requested = True
        task.status = TaskStatus.CANCELLED.value
        task.finished_at = now
        task.message = reason
        cancelled.append(task)
    if cancelled:
        db.session.commit()
    return cancelled


def delete_task(task: Task, *, actor_id: Optional[int] = None,
                commit: bool = True) -> None:
    """Soft-delete a task: it leaves every list, but stays restorable.

    A task carries its run history, log and report; "I deleted the wrong run"
    used to be unrecoverable.
    """
    if task.deleted_at is not None:
        return
    task.deleted_at = _utcnow()
    task.deleted_by = actor_id
    if commit:
        db.session.commit()


def restore_task(task: Task, *, commit: bool = True) -> Task:
    task.deleted_at = None
    task.deleted_by = None
    if commit:
        db.session.commit()
    return task


def remove_task_artifacts(task: Task) -> None:
    """Remove a task's log/staging directories from disk.

    Deliberately *not* called on soft delete: a restore that hands back a row
    whose report and log are gone is worse than no restore at all, because the
    task still looks runnable. Artifacts go only when the task is purged.

    ``task.workspace`` is a shared per-project root, so only this test id's
    subtree is removed.
    """
    workspace, test_id = task.workspace, task.test_id
    if not (workspace and test_id):
        return
    import shutil

    from ..runners import run_layout
    shutil.rmtree(run_layout.log_dir(workspace, test_id), ignore_errors=True)
    shutil.rmtree(run_layout.staging_dir(workspace, test_id), ignore_errors=True)


def purge_task(task: Task, *, commit: bool = True) -> None:
    """Delete a task for real, taking its events and artifacts with it."""
    remove_task_artifacts(task)
    db.session.delete(task)
    if commit:
        db.session.commit()
