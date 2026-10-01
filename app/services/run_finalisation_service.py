"""Atomically finalise an immutable attempt and its collaborative-safe writeback."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from ..extensions import db
from ..models import RunEvidence, Task, TaskStatus
from . import run_evidence_service as evidence
from .lanmatrix import run_writeback_service


def finalise_attempt(task_id, run_count, status, verdict, message, *, writeback=None, trusted_outcome=True) -> bool:
    try:
        task = Task.query.filter_by(id=task_id).populate_existing().with_for_update().first()
        if task is None or task.deleted_at is not None or (task.run_count or 1) != run_count:
            db.session.rollback()
            return False
        record = (RunEvidence.query.filter_by(task_id=task_id, run_count=run_count)
                  .populate_existing().with_for_update().first())
        if record is not None and record.finalised:
            db.session.rollback()
            return False
        data = evidence.read_evidence(task, run_count) if trusted_outcome else None
        if trusted_outcome:
            outcome = data["outcome"]
            if outcome is None:
                raise evidence.EvidenceError("Finalisation requires a durably sealed outcome")
            status, verdict, message = (outcome[key] for key in ("status", "verdict", "message"))
            finished = datetime.fromisoformat(outcome["finished_at"]).astimezone(timezone.utc).replace(tzinfo=None)
        else:
            if status not in (TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
                raise evidence.EvidenceError("Untrusted evidence cannot finalise a successful verdict")
            finished = datetime.now(timezone.utc).replace(tzinfo=None)
        if not TaskStatus(status).is_final:
            raise evidence.EvidenceError("A final attempt status is required")
        snapshot = SimpleNamespace(**{column.name: getattr(task, column.name) for column in Task.__table__.columns})
        snapshot.run_count = run_count
        snapshot.finished_at = finished
        snapshot.status, snapshot.result, snapshot.message = status, verdict, message
        if data is not None:
            snapshot.sil_name = data["model"]["name"]
            snapshot.sil_version = data["model"]["version"]
        result = (writeback or run_writeback_service.record_run)(snapshot, verdict, commit=False)
        if result is not None and result < 0:
            raise RuntimeError("Attempt evidence writeback failed")
        task.status, task.result, task.message, task.finished_at = status, verdict, message, finished
        if record is not None:
            record.finalised = True
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        raise
