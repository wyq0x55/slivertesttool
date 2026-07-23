"""The :class:`Task` model -- one queued/executed test run."""

from __future__ import annotations

import datetime as _dt
import enum
from typing import Optional

from ..extensions import db


class TaskStatus(str, enum.Enum):
    """Lifecycle states of a test task."""

    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_final(self) -> bool:
        return self in (TaskStatus.PASSED, TaskStatus.FAILED, TaskStatus.CANCELLED)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class Task(db.Model):
    __tablename__ = "tasks"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # Public, human-friendly identifier, e.g. "T000001".
    task_key = db.Column(db.String(16), unique=True, nullable=False, index=True)

    task_name = db.Column(db.String(200), nullable=False, default="")
    file_name = db.Column(db.String(255), nullable=False, default="")
    # Submitter label (kept for display / legacy rows). Authenticated submissions
    # also carry ``submitter_id`` linking to the lanmatrix account.
    submitter = db.Column(db.String(64), nullable=False, default="anonymous")

    # Ownership: a task now belongs to a LAN Test Matrix project and is submitted
    # by an authenticated account. Both are nullable so legacy/unscoped rows keep
    # working. Only members of ``project_id`` may view / run / download the task.
    project_id = db.Column(db.Integer, index=True, nullable=True)
    submitter_id = db.Column(db.Integer, index=True, nullable=True)

    # Silver-specific execution parameters.
    test_id = db.Column(db.String(128), nullable=False, default="")
    # Path to the ``.sil`` model to run against. For the current server-side
    # model registry this is the admin-registered *absolute* server path; the
    # legacy in-bundle flow still stores a path relative to the run directory.
    sil_relpath = db.Column(db.String(500), nullable=False, default="model.sil")
    # Display name of the chosen registered model (empty for legacy bundles).
    sil_name = db.Column(db.String(128), nullable=False, default="")

    status = db.Column(
        db.String(16), nullable=False, default=TaskStatus.QUEUED.value, index=True
    )
    progress = db.Column(db.Integer, nullable=False, default=0)
    result = db.Column(db.Text, nullable=False, default="")
    message = db.Column(db.Text, nullable=False, default="")

    # Cancellation is cooperative: the API sets this flag, the worker honours it.
    cancel_requested = db.Column(db.Boolean, nullable=False, default=False)

    workspace = db.Column(db.String(500), nullable=False, default="")
    report_path = db.Column(db.String(500), nullable=False, default="")

    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    events = db.relationship(
        "TaskEvent",
        backref="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TaskEvent.id",
    )

    # ------------------------------------------------------------------ #
    def to_dict(self, detail: bool = False) -> dict:
        data = {
            "task_id": self.task_key,
            "task_name": self.task_name,
            "file_name": self.file_name,
            "submitter": self.submitter,
            "submitter_id": self.submitter_id,
            "project_id": self.project_id,
            "test_id": self.test_id,
            "sil_name": self.sil_name,
            "status": self.status,
            # Judge verdict parsed from jdgrslt.log (PASS/FAIL/ERROR/...). This
            # is the *test* result, distinct from the execution ``status``: a run
            # can finish yet still carry a failing verdict.
            "result": self.result,
            "progress": self.progress,
            "message": self.message,
            "has_result": bool(self.report_path),
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }
        if detail:
            data["result"] = self.result
            data["sil_relpath"] = self.sil_relpath
            data["cancel_requested"] = self.cancel_requested
        return data

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Task {self.task_key} {self.status}>"


def _iso(value: Optional[_dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_dt.timezone.utc)
    return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
