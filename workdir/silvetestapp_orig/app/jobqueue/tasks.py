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
def _pooling_enabled(config) -> bool:
    return (
        bool(getattr(config, "SILVER_POOL_ENABLED", True))
        and (config.RUNNER_BACKEND or "").strip().lower() in ("silver", "mock")
    )


def get_pool(app, config):
    """Return the process-wide Silver instance pool, initialising it lazily.

    The pool's default-model getter resolves the first registered ``.sil`` model
    inside an app context, so pre-warming can open a real model when one exists.
    """
    from ..runners.silver_pool import build_driver, get_pool as _get_pool
    from ..services import model_service

    def _default_sil():
        try:
            with app.app_context():
                default = model_service.default_model()
        except Exception:  # noqa: BLE001
            return None
        if default and Path(str(default["path"])).is_file():
            return Path(str(default["path"]))
        return None

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


@huey.task()
def run_task(task_pk: int) -> None:
    app = _get_app()
    config = app.config_obj
    if _pooling_enabled(config):
        _run_task_pooled(app, config, task_pk)
    else:
        _run_task_dedicated(app, config, task_pk)


def _run_task_pooled(app, config, task_pk: int) -> None:
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
            return t is None or t.cancel_requested or TaskStatus(t.status).is_final

    instance = pool.acquire(sil_path, should_cancel=_should_cancel,
                            poll=_LICENSE_POLL_SECONDS)
    if instance is None:
        with app.app_context():
            task = db.session.get(Task, task_pk)
            if task is not None:
                _mark_cancelled(db, task)
        return

    # --- Phase 2: run, always returning the instance to the pool. ---
    with app.app_context():
        license_service.mark_busy()
        try:
            task = db.session.get(Task, task_pk)
            if task is None:
                return
            if task.cancel_requested:
                _mark_cancelled(db, task)
                return
            task.status = TaskStatus.RUNNING.value
            task.started_at = _utcnow()
            task.message = "Running on Silver."
            db.session.commit()
            event_service.emit_status(task, "running", "Running on Silver.")

            test_runner.execute(app, config, task, pool=pool, instance=instance)
        except Exception as exc:  # noqa: BLE001
            logger.exception("run_task failed for pk=%s", task_pk)
            task = db.session.get(Task, task_pk)
            if task is not None and not TaskStatus(task.status).is_final:
                task.status = TaskStatus.FAILED.value
                task.message = f"Internal error: {exc}"
                task.finished_at = _utcnow()
                db.session.commit()
        finally:
            pool.release(instance)
            license_service.mark_idle()


def _run_task_dedicated(app, config, task_pk: int) -> None:
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

        # --- Phase 1: wait for a license slot, cancellable while queued. ---
        acquired = False
        while not acquired:
            db.session.expire_all()
            task = db.session.get(Task, task_pk)
            if task is None:
                return
            if task.cancel_requested or TaskStatus(task.status).is_final:
                _mark_cancelled(db, task)
                return
            acquired = license_service.try_acquire()
            if not acquired:
                time.sleep(_LICENSE_POLL_SECONDS)

        # --- Phase 2: run, always releasing the slot. ---
        try:
            task = db.session.get(Task, task_pk)
            if task is None:
                return
            if task.cancel_requested:
                _mark_cancelled(db, task)
                return
            task.status = TaskStatus.RUNNING.value
            task.started_at = _utcnow()
            task.message = "Running on Silver."
            db.session.commit()
            event_service.emit_status(task, "running", "Running on Silver.")

            test_runner.execute(app, app.config_obj, task)
        except Exception as exc:  # noqa: BLE001
            logger.exception("run_task failed for pk=%s", task_pk)
            task = db.session.get(Task, task_pk)
            if task is not None and not TaskStatus(task.status).is_final:
                task.status = TaskStatus.FAILED.value
                task.message = f"Internal error: {exc}"
                task.finished_at = _utcnow()
                db.session.commit()
        finally:
            license_service.release()


def _mark_cancelled(db, task) -> None:
    from ..models import TaskStatus

    if not TaskStatus(task.status).is_final:
        task.status = TaskStatus.CANCELLED.value
        task.message = "Cancelled by user."
        task.finished_at = _utcnow()
        db.session.commit()
