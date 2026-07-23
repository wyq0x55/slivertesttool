"""The :class:`TaskEvent` model -- a single streamed event for a task.

Events are the backbone of realtime communication: the worker appends rows and
the SSE endpoint replays them to connected browsers by id cursor. Because they
live in the shared PostgreSQL database, streaming works across the web and
worker processes without any broker.
"""

from __future__ import annotations

import datetime as _dt
import enum

from ..extensions import db


class EventType(str, enum.Enum):
    LOG = "log"
    PROGRESS = "progress"
    WARNING = "warning"
    ERROR = "error"
    RESULT = "result"
    STATUS = "status"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class TaskEvent(db.Model):
    __tablename__ = "task_events"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    task_id = db.Column(
        db.Integer,
        db.ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = db.Column(db.String(16), nullable=False, default=EventType.LOG.value)
    message = db.Column(db.Text, nullable=False, default="")
    # Optional structured payload as a JSON string (e.g. progress value).
    payload_json = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "message": self.message,
            "payload_json": self.payload_json,
            "created_at": _iso(self.created_at),
        }


def _iso(value: _dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
