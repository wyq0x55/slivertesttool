"""Task event helpers: append events and format them for SSE.

The worker calls :func:`emit` to record log/progress/result lines; the SSE
endpoint uses :func:`fetch_since` to replay new rows to a browser and
:func:`format_sse` to serialise them into the ``text/event-stream`` wire format.
"""

from __future__ import annotations

import json
from typing import Optional

from ..extensions import db
from ..models import EventType, Task, TaskEvent, TaskStatus


def emit(
    task: Task,
    event_type: str,
    message: str = "",
    payload: Optional[dict] = None,
) -> TaskEvent:
    """Append one event to a task and commit it so streamers see it promptly."""
    event = TaskEvent(
        task_id=task.id,
        event_type=event_type,
        message=message or "",
        payload_json=json.dumps(payload, ensure_ascii=False) if payload else "",
    )
    db.session.add(event)
    db.session.commit()
    return event


def emit_log(task: Task, message: str) -> TaskEvent:
    return emit(task, EventType.LOG.value, message)


def emit_logs(task: Task, messages) -> int:
    """Append many log events in a SINGLE commit (write-batching).

    A running test can produce dozens of console lines per poll cycle. Emitting
    each with its own INSERT+COMMIT (see :func:`emit`) causes heavy write
    amplification (one transaction per line + WAL churn). The console tailer
    hands a whole chunk here so the poll's lines are persisted in one round trip.
    Returns the number of events written.
    """
    msgs = [m for m in (messages or []) if m is not None]
    if not msgs:
        return 0
    db.session.add_all([
        TaskEvent(
            task_id=task.id,
            event_type=EventType.LOG.value,
            message=(m or ""),
            payload_json="",
        )
        for m in msgs
    ])
    db.session.commit()
    return len(msgs)


def emit_progress(task: Task, value: int) -> TaskEvent:
    value = max(0, min(100, int(value)))
    task.progress = value
    db.session.add(task)
    db.session.commit()
    return emit(task, EventType.PROGRESS.value, f"{value}%", {"value": value})


def emit_error(task: Task, message: str) -> TaskEvent:
    return emit(task, EventType.ERROR.value, message)


def emit_result(task: Task, status: str, message: str = "") -> TaskEvent:
    return emit(task, EventType.RESULT.value, message, {"status": status})


def emit_status(task: Task, status: str, message: str = "") -> TaskEvent:
    return emit(task, EventType.STATUS.value, message, {"status": status})


def prune_task_events(task_pk: int, *, keep_last: int = 5000) -> int:
    """Delete all but the most recent ``keep_last`` events of one task.

    ``TaskEvent`` rows accumulate for the lifetime of a task (a chatty run emits
    thousands of LOG rows) and were previously only ever removed when the whole
    project was deleted, so the table grows unbounded. This bounds a single
    task's event history to the newest ``keep_last`` rows.

    Intended for OFF-HOT-PATH maintenance (a periodic sweep or an admin action),
    NOT for the finalize path: an SSE client may still be draining the tail right
    after a task turns final, and deleting rows it is about to replay would create
    gaps. Returns the number of rows deleted.
    """
    if keep_last < 0:
        keep_last = 0
    # Find the id cut-off: the id of the ``keep_last``-th newest event. Anything
    # with a smaller id is surplus. One indexed scan + one ranged DELETE.
    cutoff_row = (
        TaskEvent.query
        .with_entities(TaskEvent.id)
        .filter(TaskEvent.task_id == task_pk)
        .order_by(TaskEvent.id.desc())
        .offset(keep_last)
        .limit(1)
        .first()
    )
    if cutoff_row is None:
        return 0  # fewer than keep_last events; nothing to prune
    cutoff_id = cutoff_row[0]
    deleted = (
        TaskEvent.query
        .filter(TaskEvent.task_id == task_pk, TaskEvent.id <= cutoff_id)
        .delete(synchronize_session=False)
    )
    db.session.commit()
    return int(deleted or 0)


_FINAL_STATUSES = tuple(
    s.value for s in TaskStatus if s.is_final
)


def prune_all_task_events(*, keep_last: int = 5000,
                          only_final: bool = True) -> dict:
    """Trim every task's event history to the newest ``keep_last`` rows.

    Used by the daily worker sweep and the admin-console button. Only tasks that
    actually exceed ``keep_last`` are touched (one grouped COUNT locates them), so
    a quiet database does no DELETE work at all.

    ``only_final`` (default) skips QUEUED/RUNNING tasks: a live task is still
    appending events and may have an SSE client mid-replay, so its history is left
    alone until it reaches a terminal state. Returns a summary dict
    ``{"tasks": <#tasks pruned>, "deleted": <#rows deleted>}``.
    """
    if keep_last < 0:
        keep_last = 0
    # Locate the offending tasks in ONE grouped query instead of scanning every
    # task: a HAVING COUNT(*) > keep_last returns only tasks with surplus rows.
    q = (
        db.session.query(TaskEvent.task_id)
        .group_by(TaskEvent.task_id)
        .having(db.func.count(TaskEvent.id) > keep_last)
    )
    if only_final:
        # Restrict to terminal tasks by joining against the tasks table.
        q = q.join(Task, Task.id == TaskEvent.task_id).filter(
            Task.status.in_(_FINAL_STATUSES))
    task_ids = [row[0] for row in q.all()]

    total_deleted = 0
    pruned_tasks = 0
    for task_pk in task_ids:
        deleted = prune_task_events(task_pk, keep_last=keep_last)
        if deleted:
            pruned_tasks += 1
            total_deleted += deleted
    return {"tasks": pruned_tasks, "deleted": total_deleted}


def fetch_since(task_pk: int, last_id: int, limit: int = 200) -> list[TaskEvent]:
    """Return up to ``limit`` events for a task with ``id > last_id``."""
    return (
        TaskEvent.query.filter(
            TaskEvent.task_id == task_pk, TaskEvent.id > last_id
        )
        .order_by(TaskEvent.id.asc())
        .limit(limit)
        .all()
    )


def format_sse(event: TaskEvent) -> str:
    """Serialise a :class:`TaskEvent` into one SSE frame."""
    data = {
        "id": event.id,
        "message": event.message,
    }
    payload = event.payload_json
    if payload:
        try:
            data.update(json.loads(payload))
        except (ValueError, TypeError):
            pass
    body = json.dumps(data, ensure_ascii=False)
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {body}\n\n"
