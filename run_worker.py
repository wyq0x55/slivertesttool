"""Start the Huey worker that executes queued test tasks.

    python run_worker.py

Consumes tasks from the PostgreSQL-backed Huey queue and runs them via the
Silver backend. In split-process deployments, run ``python manage.py bootstrap``
once before starting this worker.

Run exactly one worker. It takes a PostgreSQL session advisory lock before
resetting the license counter or recovering in-flight work. A second worker
exits before it can change that state.

Pre-warmed pool
---------------
When ``SILVER_POOL_ENABLED`` is on (the default), the worker launches
``license_limit`` empty Silver instances at start-up (see
``app.runners.silver_pool``). Each open instance holds one license, so the
licenses are pre-empted immediately and every subsequent test reuses a warm
instance instead of paying the Silver process-launch cost. A background
reconciler keeps the pool size in step with the runtime-adjustable license
limit, and the pool is disposed cleanly on shutdown.
"""

from __future__ import annotations

import atexit
import signal
import logging
import threading

from app.config import Config
from app.logging_setup import configure as configure_logging

# Before the tasks module (which builds the Flask app lazily) so the worker
# claims the "worker" role and its own rotating file.
configure_logging("worker", Config)

# Importing the tasks module registers ``run_task`` with the shared huey instance.
# The single worker-side Flask app is built lazily and cached inside ``tasks``
# (via ``tasks._get_app()``); ``main()`` materialises the side-effect-light app
# exactly once. Persistent bootstrap is owned by run.py or ``manage.py bootstrap``;
# the worker must never migrate schema/files itself.
from app.jobqueue import tasks  # noqa: F401
from app.jobqueue.huey_app import huey
from app.runners import process_guard

logger = logging.getLogger("silvetestapp.worker")

# Kill-on-close job the worker places itself in (Windows only). Every Silver
# process the worker starts inherits membership, so the kernel terminates them
# when this process ends -- including a hard kill that never reaches the
# ``atexit`` pool dispose. Kept referenced for the process lifetime.
_silver_job: process_guard.KillOnCloseJob | None = None


def _sweep_silver(config) -> None:
    """Force-kill leftover Silver processes so no license stays held on exit."""
    try:
        if (getattr(config, "RUNNER_BACKEND", "mock") or "").lower() != "silver":
            return
        if not getattr(config, "SILVER_KILL_ON_EXIT", False):
            return
        from app.runners.silver_cleanup import force_kill_silver_processes
        force_kill_silver_processes(getattr(config, "SILVER_PROCESS_IMAGE_NAMES", None))
    except Exception:  # noqa: BLE001 - cleanup must never raise on shutdown
        logger.exception("Silver exit sweep failed")


def _reconcile_loop(app, config, stop: threading.Event) -> None:
    """Keep the pool sized to the (runtime-adjustable) license limit.

    The reconcile interval, eager pre-warm flag and pool enable switch are all
    runtime-adjustable from the admin console (「授权 / 并发」); each is re-read on
    every tick (with a safe fallback to the ``.env``-derived Config default) so
    an admin's change is picked up live without restarting the worker. When the
    pool is disabled live, the target is driven to 0 so the pooled instances are
    disposed and their licenses released.
    """
    from app.jobqueue.tasks import get_pool
    from app.services import license_service, runtime_config

    pool = get_pool(app, config)

    while not stop.is_set():
        interval = 5.0
        try:
            with app.app_context():
                limit = license_service.get_limit()
                draining = license_service.is_draining()
                pool_enabled = runtime_config.get_bool("silver_pool_enabled")
                prewarm = runtime_config.get_bool("silver_pool_prewarm")
                interval = runtime_config.get_float("silver_pool_reconcile_seconds")
            pool.set_target(limit if pool_enabled and not draining else 0)
            if pool_enabled and prewarm and not draining:
                pool.prewarm()
        except Exception:  # noqa: BLE001 - reconciler must never die
            logger.exception("Pool reconcile iteration failed")
            interval = float(getattr(config, "SILVER_POOL_RECONCILE_SECONDS", 5) or 5)
        stop.wait(max(0.5, interval))


def _bind_to_kill_on_close_job() -> None:
    global _silver_job
    if _silver_job is not None:
        return
    job = process_guard.KillOnCloseJob.create()
    if job is not None and job.assign_current_process():
        _silver_job = job



def _require_worker_ownership() -> None:
    """Stop startup once this process no longer owns the runtime lock."""
    from app.services import worker_lock

    if not worker_lock.ownership_is_current() or process_guard.is_shutting_down():
        logger.error("Worker no longer holds the runtime lock; refusing further startup.")
        raise SystemExit(1)


def recover_owned_work(app, prewarm=None):
    """Recover interrupted work only while this process still owns the lock."""
    _require_worker_ownership()
    with app.app_context():
        return _recover_owned_work(app, prewarm)


def _recover_owned_work(app, prewarm=None):
    from app.services import worker_lock
    from app.services.ai import jobs as ai_jobs

    _require_worker_ownership()
    with worker_lock.ownership_guard() as owned:
        if not owned:
            raise SystemExit(1)
        ai_jobs.recover(tasks.publish_ai_generation, startup=True)
    _require_worker_ownership()
    recovery = tasks.recover_run_attempts(startup=True)
    if recovery["errors"]:
        logger.warning("Startup run recovery failed: %s", recovery["errors"])
    _require_worker_ownership()
    from app.jobqueue import ut_tasks
    ut_recovery = ut_tasks.recover_interrupted_jobs()
    if ut_recovery["interrupted"] or ut_recovery["stopped"]:
        logger.warning("Unit-test jobs interrupted by the previous worker: %s", ut_recovery)
    _require_worker_ownership()
    if prewarm is not None:
        prewarm()


def begin_worker_ownership(app):
    """Take the single-worker lock before resetting shared runtime state.

    A second worker fails here, before the license counter is cleared or any
    in-flight run is treated as interrupted.
    """
    from app.services import license_service, worker_lock

    with app.app_context():
        lock = worker_lock.acquire()
        try:
            with worker_lock.ownership_guard() as owned:
                if not owned:
                    raise SystemExit(1)
                license_service.reset_in_use()
        except BaseException:
            lock.release()
            raise
        return lock


def main() -> None:
    # Before any Silver instance can be launched, so all of them join the job.
    _bind_to_kill_on_close_job()
    app = tasks._get_app()
    config = app.config_obj
    from app.services import license_service, worker_lock

    stop_reconcile = threading.Event()
    try:
        lock = begin_worker_ownership(app)
    except worker_lock.WorkerAlreadyRunning:
        logger.error(
            "Another worker already holds the runtime lock; "
            "exiting before license reset or run recovery."
        )
        raise SystemExit(1)
    # Release only at process exit, after the later pool-shutdown hook. atexit
    # runs last-registered first, and the pool hook is registered below.
    atexit.register(lock.release)
    consumer_box = {}

    def _lost_runtime_lock() -> None:
        process_guard.begin_shutdown()
        stop_reconcile.set()
        logger.critical(
            "Lost the worker runtime lock; stopping this worker without acquiring another."
        )
        consumer = consumer_box.get("consumer")
        if consumer is None:
            return
        try:
            consumer._handle_stop_signal(signal.SIGTERM, None)
        except Exception:
            logger.exception("Could not stop the consumer after losing the worker lock")

    lock.supervise(_lost_runtime_lock)

    # The previous owner is gone. Only this process may now reclaim its slots
    # and decide which in-flight attempts were interrupted. Each stage stops
    # if supervision reports that the lock session has disappeared.
    recover_owned_work(app)

    # Pool infrastructure is started whenever the runner backend supports it, so
    # the reconcile loop is always present to honour a *live* enable/disable of
    # ``SILVER_POOL_ENABLED`` from the admin console (it drives the pool target to
    # 0 when disabled). The live flag -- not this start-up check -- decides
    # whether instances are actually pre-warmed.
    backend = (config.RUNNER_BACKEND or "").strip().lower()
    pool_supported = backend in ("silver", "mock")
    if pool_supported:
        from app.jobqueue.tasks import get_pool
        from app.services import runtime_config

        pool = get_pool(app, config)

        # Dispose all instances owned by this worker on process exit. The
        # machine-wide image-name sweep is an explicit emergency fallback.
        def _shutdown_pool() -> None:
            process_guard.begin_shutdown()
            stop_reconcile.set()
            try:
                pool.shutdown()
            except Exception:  # noqa: BLE001
                logger.exception("Pool shutdown failed")
            _sweep_silver(config)

        atexit.register(_shutdown_pool)

        # Read the live pool switches for the eager start-up pre-warm.
        with app.app_context():
            pool_enabled = runtime_config.get_bool("silver_pool_enabled")
            prewarm = runtime_config.get_bool("silver_pool_prewarm")
            limit = license_service.get_limit()
            draining = license_service.is_draining()

        if pool_enabled and prewarm and not draining:
            _require_worker_ownership()
            pool.set_target(limit)
            warmed = pool.prewarm()
            print(f"Pre-warmed {warmed} Silver instance(s); pool={pool.stats()}")

        threading.Thread(
            target=_reconcile_loop,
            args=(app, config, stop_reconcile),
            daemon=True,
        ).start()

    print(f"Worker starting: {Config.HUEY_WORKERS} threads, "
          f"backend={Config.RUNNER_BACKEND}, "
          f"pool={'supported' if pool_supported else 'off'}")
    consumer = huey.create_consumer(
        workers=Config.HUEY_WORKERS,
        worker_type="thread",
    )
    # run.py stops this worker with CTRL_BREAK_EVENT on Windows (there is no
    # SIGTERM to deliver). Treat it like Huey's SIGTERM: stop consuming and
    # exit normally, so the atexit pool shutdown disposes every Silver instance.
    def _on_break(signum, frame) -> None:
        process_guard.begin_shutdown()
        consumer._handle_stop_signal(signum, frame)

    process_guard.install_break_handler(_on_break)
    consumer_box["consumer"] = consumer
    if process_guard.is_shutting_down():
        raise SystemExit(1)
    try:
        consumer.run()
    finally:
        stop_reconcile.set()

if __name__ == "__main__":
    main()
