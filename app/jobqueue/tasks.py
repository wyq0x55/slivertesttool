"""Huey task definitions (run in the worker process).

The single ``run_task`` task waits for a license slot (via the DB-backed gate),
marks the task RUNNING, delegates execution to :mod:`app.runners.test_runner`,
and always releases its slot afterwards. A Flask app + context is created lazily
so the worker shares the exact same models, database and configuration as the
web process.
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from pathlib import Path

from huey import crontab

from .huey_app import huey

logger = logging.getLogger("silvetestapp.tasks")

_LICENSE_POLL_SECONDS = 0.5

# Lazily-created worker-side Flask application (avoids an import cycle with the
# app factory and keeps a single app per worker process).
_worker_app = None


def _get_app():
    global _worker_app
    if _worker_app is None:
        from .. import create_app

        _worker_app = create_app()
    return _worker_app


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Pre-warmed Silver instance pool (worker-process singleton)
# --------------------------------------------------------------------------- #
def _pooling_enabled(config, app=None) -> bool:
    """Whether the pre-warmed pool path is used for a run.

    ``SILVER_POOL_ENABLED`` is runtime-adjustable from the admin console; when an
    ``app`` is supplied we read the live override (inside an app context), else we
    fall back to the ``.env``-derived Config default. The runner backend still
    gates pooling (only the ``silver``/``mock`` backends support it) and is fixed
    at start-up.
    """
    enabled = bool(getattr(config, "SILVER_POOL_ENABLED", True))
    if app is not None:
        try:
            from ..services import runtime_config

            with app.app_context():
                enabled = runtime_config.get_bool("silver_pool_enabled")
        except Exception:  # noqa: BLE001 - defensive: fall back to the static default
            pass
    return enabled and (config.RUNNER_BACKEND or "").strip().lower() in ("silver", "mock")


def get_pool(app, config):
    """Return the process-wide Silver instance pool, initialising it lazily.

    Pre-warm needs only a valid seed model to start Silver early. The seed comes
    from the project model table (current models first); each real task still
    re-opens its own ``sil_path`` before execution, so the seed has no model
    selection semantics.
    """
    from ..runners.silver_pool import build_driver, get_pool as _get_pool

    def _default_sil():
        try:
            with app.app_context():
                from ..models import ProjectModel
                row = (
                    ProjectModel.query
                    .filter(
                        ProjectModel.deprecated_at.is_(None),
                        ProjectModel.sil_path != "",
                    )
                    .order_by(ProjectModel.is_current.desc(), ProjectModel.id.asc())
                    .first()
                )
                path = Path(row.sil_path) if row and row.sil_path else None
        except Exception:  # noqa: BLE001 - prewarm may defer safely
            return None
        return path if path is not None and path.is_file() else None

    driver = build_driver(
        config.RUNNER_BACKEND,
        gui=bool(getattr(config, "SILVER_GUI", False)),
    )
    pool = _get_pool(driver, Path(config.POOL_DIR), _default_sil)
    pool.set_target(_current_limit(app))
    return pool


def _current_limit(app) -> int:
    from ..services import license_service

    with app.app_context():
        return license_service.get_limit()


def _claim_run(database, task_pk: int, expected_run_count: int):
    from ..models import Task, TaskStatus

    task = (Task.query.filter_by(id=task_pk, run_count=expected_run_count,
                                 status=TaskStatus.QUEUED.value, deleted_at=None,
                                 cancel_requested=False)
            .populate_existing().with_for_update().first())
    if task is None:
        database.session.rollback()
        return None
    task.status = TaskStatus.RUNNING.value
    task.started_at = _utcnow()
    task.message = "Running on Silver."
    database.session.commit()
    return task


@huey.task()
def run_task(task_pk: int, expected_run_count: int | None = None) -> None:
    app = _get_app()
    config = app.config_obj
    if _pooling_enabled(config, app):
        _run_task_pooled(app, config, task_pk, expected_run_count)
    else:
        _run_task_dedicated(app, config, task_pk, expected_run_count)


def _run_task_pooled(app, config, task_pk: int, expected_run_count=None) -> None:
    """Execute a task on a pre-warmed, reusable pooled Silver instance."""
    from ..extensions import db
    from ..models import Task, TaskStatus
    from ..runners import test_runner
    from ..services import event_service, license_service

    pool = get_pool(app, config)

    with app.app_context():
        task = db.session.get(Task, task_pk)
        if task is None:
            logger.error("Task pk=%s vanished before execution", task_pk)
            return
        if TaskStatus(task.status) != TaskStatus.QUEUED:
            logger.info("Task %s no longer queued (%s); skipping",
                        task.task_key, task.status)
            return
        if expected_run_count is None:
            expected_run_count = task.run_count or 1
        if task.deleted_at is not None or task.run_count != expected_run_count:
            return
        from ..runners import run_layout
        sil_ref = Path(task.sil_relpath)
        # Model path is normally absolute (admin registry); the legacy in-bundle
        # flow resolves it against the enqueue-time staging scripts.
        staged = run_layout.staging_dir(task.workspace, task.test_id) / task.test_id
        sil_path = sil_ref if sil_ref.is_absolute() else (staged / sil_ref).resolve()

    # --- Phase 1: borrow a pooled instance, cancellable while queued. ---
    def _should_cancel() -> bool:
        with app.app_context():
            db.session.expire_all()
            t = db.session.get(Task, task_pk)
            return (t is None or t.cancel_requested or t.deleted_at is not None
                    or t.status != TaskStatus.QUEUED.value or t.run_count != expected_run_count)

    instance = pool.acquire(sil_path, should_cancel=_should_cancel,
                            poll=_LICENSE_POLL_SECONDS)
    if instance is None:
        with app.app_context():
            task = db.session.get(Task, task_pk)
            if (task is not None and task.run_count == expected_run_count
                    and task.status == TaskStatus.QUEUED.value and task.cancel_requested):
                _mark_cancelled(db, task)
        return

    # --- Phase 2: run, always returning the instance to the pool. ---
    with app.app_context():
        license_service.mark_busy()
        try:
            task = _claim_run(db, task_pk, expected_run_count)
            if task is None:
                return
            event_service.emit_status(task, "running", "Running on Silver.")

            test_runner.execute(app, config, task, pool=pool, instance=instance)
        except Exception as exc:  # noqa: BLE001
            logger.exception("run_task failed for pk=%s", task_pk)
            task = db.session.get(Task, task_pk)
            if (task is not None and task.run_count == expected_run_count
                    and not TaskStatus(task.status).is_final):
                task.status = TaskStatus.FAILED.value
                task.message = f"Internal error: {exc}"
                task.finished_at = _utcnow()
                db.session.commit()
        finally:
            pool.release(instance)
            license_service.mark_idle()


def _run_task_dedicated(app, config, task_pk: int, expected_run_count=None) -> None:
    """Classic path: launch a dedicated Silver instance per task."""
    from ..extensions import db
    from ..models import Task, TaskStatus
    from ..runners import test_runner
    from ..services import event_service, license_service

    with app.app_context():
        task = db.session.get(Task, task_pk)
        if task is None:
            logger.error("Task pk=%s vanished before execution", task_pk)
            return
        if TaskStatus(task.status) != TaskStatus.QUEUED:
            logger.info("Task %s no longer queued (%s); skipping", task.task_key, task.status)
            return
        if expected_run_count is None:
            expected_run_count = task.run_count or 1
        if task.deleted_at is not None or task.run_count != expected_run_count:
            return

        # --- Phase 1: wait for a license slot, cancellable while queued. ---
        acquired = False
        while not acquired:
            db.session.expire_all()
            task = db.session.get(Task, task_pk)
            if task is None:
                return
            if (task.deleted_at is not None or task.run_count != expected_run_count
                    or task.status != TaskStatus.QUEUED.value):
                return
            if task.cancel_requested:
                _mark_cancelled(db, task)
                return
            acquired = license_service.try_acquire()
            if not acquired:
                time.sleep(_LICENSE_POLL_SECONDS)

        # --- Phase 2: run, always releasing the slot. ---
        # Reserve a bounded, reused run-folder slot so the classic path also
        # labels its directory ``inst_<n>`` (recycled on release) instead of a
        # unique ``inst_dedicated_<task_id>`` per task that piles up on disk.
        from ..runners.slots import dedicated_allocator
        slot = dedicated_allocator().acquire()
        try:
            task = _claim_run(db, task_pk, expected_run_count)
            if task is None:
                return
            event_service.emit_status(task, "running", "Running on Silver.")

            test_runner.execute(app, app.config_obj, task, dedicated_slot=slot)
        except Exception as exc:  # noqa: BLE001
            logger.exception("run_task failed for pk=%s", task_pk)
            task = db.session.get(Task, task_pk)
            if (task is not None and task.run_count == expected_run_count
                    and not TaskStatus(task.status).is_final):
                task.status = TaskStatus.FAILED.value
                task.message = f"Internal error: {exc}"
                task.finished_at = _utcnow()
                db.session.commit()
        finally:
            dedicated_allocator().release(slot)
            license_service.release()


def publish_ai_generation(draft_pk: int) -> None:
    from ..extensions import db
    from ..models import AiDraft
    from ..services.ai import jobs

    draft = db.session.get(AiDraft, draft_pk)
    if draft is not None and draft.status == AiDraft.STATUS_RUNNING:
        attempt = (jobs.metadata(draft).get("job") or {}).get("attempt")
        run_ai_generation(draft_pk, attempt)


@huey.task()
def run_ai_generation(draft_pk: int, attempt: str | None = None) -> None:
    """Consume a fenced generation attempt without taking a Silver license."""
    app = _get_app()
    with app.app_context():
        import json

        from ..extensions import db
        from ..models import AiDraft
        from ..services.ai import scenarios as ai_scenarios
        from ..services.ai import jobs as ai_jobs

        draft = db.session.get(AiDraft, draft_pk)
        if draft is None or draft.status != AiDraft.STATUS_RUNNING:
            return

        if attempt is None:
            attempt = (ai_jobs.metadata(draft).get("job") or {}).get("attempt")
        if attempt is None:
            attempt = ai_jobs.prepare(draft)
            db.session.commit()
        if not ai_jobs.acquire_slot():
            if not huey.immediate:
                run_ai_generation.schedule(args=(draft_pk, attempt), delay=2)
            return
        checkpoint_token = None
        try:
            if not ai_jobs.claim(draft_pk, attempt):
                return
            draft = db.session.get(AiDraft, draft_pk)
            payload = json.loads(draft.input_json) if draft.input_json else {}
            scenario = draft.scenario
            checkpoint_token = ai_jobs.install_checkpoint(lambda: ai_jobs.touch(draft_pk, attempt))
            result = ai_scenarios.run_scenario(
                scenario, payload, on_event=lambda event: ai_jobs.touch(draft_pk, attempt, event))
            ai_jobs.checkpoint()
            ai_jobs.finish(draft_pk, attempt, output=result.output,
                           result_meta={"model": result.model, "rounds": result.rounds,
                                        "usage": result.usage, "log": result.log})
        except ai_jobs.AttemptStopped:
            db.session.rollback()
        except Exception as exc:
            logger.exception("AI generation failed for draft=%s", draft_pk)
            db.session.rollback()
            ai_jobs.finish(draft_pk, attempt, error=str(exc))
        finally:
            if checkpoint_token is not None:
                ai_jobs.reset_checkpoint(checkpoint_token)
            ai_jobs.release_slot()


@huey.periodic_task(crontab(minute="*"))
def recover_ai_generation_job() -> None:
    app = _get_app()
    with app.app_context():
        from ..services.ai import jobs
        jobs.recover(publish_ai_generation)


def recover_run_attempts() -> dict:
    """Publish pinned approved attempts without executing within the DB lock."""
    from ..services.run_evidence_service import recover_queued_attempts

    pending = {tuple(task.args) for task in huey.pending(limit=1000)
               if isinstance(task, run_task.task_class)}

    def enqueue(task_id, run_count):
        message = run_task.s(task_id, run_count)
        huey.storage.enqueue(huey.serialize_task(message), message.priority)

    return recover_queued_attempts(enqueue, is_pending=lambda task_id, run_count: (task_id, run_count) in pending)


@huey.periodic_task(crontab(minute="*"))
def recover_run_attempts_job() -> None:
    app = _get_app()
    with app.app_context():
        result = recover_run_attempts()
        if result["errors"]:
            logger.warning("Run publication recovery failed: %s", result["errors"])


@huey.periodic_task(crontab(hour="3", minute="0"))
def prune_task_events_job() -> None:
    """Daily off-peak sweep that bounds ``TaskEvent`` growth.

    Runs on the Huey consumer's periodic scheduler (03:00 local). The retention
    count is read live from ``runtime_config`` so an admin can retune it without a
    restart; only terminal tasks are trimmed so a live run's stream is untouched.
    """
    app = _get_app()
    from ..extensions import db
    from ..services import event_service, runtime_config

    with app.app_context():
        try:
            keep_last = runtime_config.get_int("task_event_retention")
        except Exception:  # noqa: BLE001 - fall back to the static default
            keep_last = int(getattr(app.config_obj, "TASK_EVENT_RETENTION", 5000))
        try:
            summary = event_service.prune_all_task_events(
                keep_last=keep_last, only_final=True)
        except Exception:  # noqa: BLE001 - maintenance must never crash the worker
            db.session.rollback()
            logger.exception("Task-event prune sweep failed")
            return
    logger.info(
        "Task-event prune sweep: trimmed %s task(s), deleted %s row(s) "
        "(keep_last=%s)", summary["tasks"], summary["deleted"], keep_last)


def _mark_cancelled(db, task) -> None:
    from ..models import TaskStatus

    if not TaskStatus(task.status).is_final:
        task.status = TaskStatus.CANCELLED.value
        task.message = "Cancelled by user."
        task.finished_at = _utcnow()
        db.session.commit()
