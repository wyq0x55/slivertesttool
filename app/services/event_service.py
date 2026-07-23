"""Task event helpers: append events and format them for SSE.

The worker calls :func:`emit` to record log/progress/result lines; the SSE
endpoint uses :func:`fetch_since` to replay new rows to a browser and
:func:`format_sse` to serialise them into the ``text/event-stream`` wire format.
"""

from __future__ import annotations

import json
from typing import Optional

from ..extensions import db
from ..models import EventType, Task, TaskEvent


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
