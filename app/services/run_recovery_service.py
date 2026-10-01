"""Bounded recovery of pinned attempts without executing a runner."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..extensions import db
from ..models import RunEvidence, Setting, Task, TaskStatus
from . import run_evidence_service as evidence


def _validate_limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("Recovery limit must be between 1 and 1000")


def _scan_attempts(status, limit):
    """Commit a circular keyset page before callbacks, in a separate session.

    Skips, callback rollbacks and process death cannot erase scan progress.
    A crash before dispatch leaves attempts eligible for a subsequent wrap.
    Only the cursor is written here; callbacks own all task state changes.
    """
    key = f"run_recovery_cursor_{status}"
    with Session(db.engine) as cursor_session, cursor_session.begin():
        cursor_session.execute(insert(Setting).values(key=key, value="0")
                               .on_conflict_do_nothing(index_elements=[Setting.key]))
        setting = cursor_session.execute(
            select(Setting).where(Setting.key == key).with_for_update()
        ).scalar_one()
        try:
            cursor = max(0, int(setting.value))
        except (TypeError, ValueError):
            cursor = 0
        candidates = (select(Task.id, Task.run_count)
                      .where(Task.status == status, Task.deleted_at.is_(None),
                             Task.cancel_requested.is_(False))
                      .order_by(Task.id))
        page = list(cursor_session.execute(candidates.where(Task.id > cursor).limit(limit)))
        if cursor and len(page) < limit:
            page.extend(cursor_session.execute(
                candidates.where(Task.id <= cursor).limit(limit - len(page))))
        setting.value = str(page[-1][0]) if page else "0"
        return [(task_id, run_count) for task_id, run_count in page]


def _current_attempt(identity, status, *, lock=False):
    query = Task.query.filter_by(id=identity[0], run_count=identity[1], status=status,
                                 deleted_at=None, cancel_requested=False).populate_existing()
    if lock:
        query = query.with_for_update(skip_locked=True)
    return query.first()


def _error(result, identity, exc):
    result["errors"].append({"task_id": identity[0], "run_count": identity[1], "error": str(exc)})


def recover_queued_attempts(enqueue, *, is_pending, limit=100):
    """Publish at most limit pinned queued attempts through an asynchronous callback.

    Both callbacks accept (task_id, run_count). Publication is at least once;
    the queue and atomic worker claim must deduplicate that attempt identity.
    Call with a clean application session; per-attempt locks are rolled back.
    """
    _validate_limit(limit)
    result = {"recovered": [], "skipped": [], "errors": []}
    for identity in _scan_attempts(TaskStatus.QUEUED.value, limit):
        try:
            task = _current_attempt(identity, TaskStatus.QUEUED.value, lock=True)
            if task is None:
                result["skipped"].append(identity)
                continue
            record = db.session.get(RunEvidence, identity, populate_existing=True)
            if record is not None and (record.outcome_sha256 or getattr(record, "finalised", False)):
                result["skipped"].append(identity)
                continue
            data = evidence.read_evidence(task, identity[1], max_log_bytes=0)
            if data["outcome"] is not None or is_pending(*identity):
                result["skipped"].append(identity)
                continue
            case = evidence.attempt_dir(task, identity[1]) / "inputs" / task.test_id
            if any(case.glob("testcase_*.json")):
                from .run_validation_service import validate_run_directory

                validate_run_directory(case)
            enqueue(*identity)
            result["recovered"].append(identity)
        except Exception as exc:
            _error(result, identity, exc)
        finally:
            db.session.rollback()
    return result


def _outcome_arguments(outcome):
    if not isinstance(outcome, dict):
        raise evidence.EvidenceError("Sealed outcome must be an object")
    status, verdict, message = (outcome.get(key) for key in ("status", "verdict", "message"))
    if (status not in (TaskStatus.PASSED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value)
            or not isinstance(verdict, str) or not verdict.strip() or not isinstance(message, str)):
        raise evidence.EvidenceError("Sealed outcome must contain a terminal status, verdict and message")
    if ((status == TaskStatus.PASSED.value) != verdict.upper().startswith("PASS")
            or (status == TaskStatus.CANCELLED.value) != (verdict.upper() == "CANCELLED")):
        raise evidence.EvidenceError("Sealed outcome status and verdict disagree")
    return status, verdict, message


def recover_interrupted_attempts(finalise, *, startup=False, limit=100):
    """Reconcile trusted sealed RUNNING outcomes through the atomic finaliser.

    finalise(task_id, run_count, status_string, verdict, message) returns bool
    and owns task locking, terminal state, writeback and finalisation markers.
    Startup must run before workers start: only then may an interrupted attempt
    with no committed outcome digest be failed for manual retry, using
    trusted_outcome=False. Periodic scans leave live unsealed attempts alone.
    Hash-bound but corrupt outcomes are reported without bypassing their seal.
    """
    _validate_limit(limit)
    if not isinstance(startup, bool):
        raise ValueError("Recovery startup must be a boolean")
    result = {"recovered": [], "skipped": [], "errors": []}
    for identity in _scan_attempts(TaskStatus.RUNNING.value, limit):
        try:
            task = _current_attempt(identity, TaskStatus.RUNNING.value)
            record = db.session.get(RunEvidence, identity, populate_existing=True) if task else None
            if record is None or not record.manifest_sha256 or getattr(record, "finalised", False):
                result["skipped"].append(identity)
                continue
            arguments = None
            try:
                data = evidence.read_evidence(task, identity[1], max_log_bytes=0)
                if data["outcome"] is not None:
                    arguments = _outcome_arguments(data["outcome"])
                elif record.outcome_sha256:
                    raise evidence.EvidenceError("Committed sealed outcome is missing")
            except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
                _error(result, identity, exc)
                if record.outcome_sha256 or not startup:
                    continue
            if arguments is not None:
                changed = finalise(*identity, *arguments)
            elif startup:
                changed = finalise(*identity, TaskStatus.FAILED.value, "ERROR",
                                   "Interrupted before a trusted outcome was sealed; manual retry required.",
                                   trusted_outcome=False)
            else:
                result["skipped"].append(identity)
                continue
            result["recovered" if changed else "skipped"].append(identity)
        except Exception as exc:
            _error(result, identity, exc)
        finally:
            db.session.rollback()
    return result
