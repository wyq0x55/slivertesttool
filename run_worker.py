"""Start the Huey worker that executes queued test tasks.

    python run_worker.py

Consumes tasks from the PostgreSQL-backed Huey queue and runs them via the
Silver backend.

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
import logging
import threading

from app.config import Config
from app.logging_setup import configure as configure_logging

# Before the tasks module (which builds the Flask app lazily) so the worker
# claims the "worker" role and its own rotating file.
configure_logging("worker", Config)

# Importing the tasks module registers ``run_task`` with the shared huey instance.
# The single worker-side Flask app is built lazily and cached inside ``tasks``
# (via ``tasks._get_app()``); ``main()`` materialises it exactly once so tables
# exist and the license settings are seeded. Building it here as well would
# construct a second, throwaway app (double Flask initialisation), so we don't.
from app.jobqueue import tasks  # noqa: F401
from app.jobqueue.huey_app import huey

logger = logging.getLogger("silvetestapp.worker")


def _sweep_silver(config) -> None:
    """Force-kill leftover Silver processes so no license stays held on exit."""
    try:
        if (getattr(config, "RUNNER_BACKEND", "mock") or "").lower() != "silver":
            return
        if not getattr(config, "SILVER_KILL_ON_EXIT", True):
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
                pool_enabled = runtime_config.get_bool("silver_pool_enabled")
                prewarm = runtime_config.get_bool("silver_pool_prewarm")
                interval = runtime_config.get_float("silver_pool_reconcile_seconds")
            pool.set_target(limit if pool_enabled else 0)
            if pool_enabled and prewarm:
                pool.prewarm()
        except Exception:  # noqa: BLE001 - reconciler must never die
            logger.exception("Pool reconcile iteration failed")
            interval = float(getattr(config, "SILVER_POOL_RECONCILE_SECONDS", 5) or 5)
        stop.wait(max(0.5, interval))


def main() -> None:
    app = tasks._get_app()
    config = app.config_obj
    from app.services import license_service

    # Recover the in-use counter in case a previous worker crashed mid-run.
    with app.app_context():
        license_service.reset_in_use()

    # Pool infrastructure is started whenever the runner backend supports it, so
    # the reconcile loop is always present to honour a *live* enable/disable of
    # ``SILVER_POOL_ENABLED`` from the admin console (it drives the pool target to
    # 0 when disabled). The live flag -- not this start-up check -- decides
    # whether instances are actually pre-warmed.
    backend = (config.RUNNER_BACKEND or "").strip().lower()
    pool_supported = backend in ("silver", "mock")
    stop_reconcile = threading.Event()

    if pool_supported:
        from app.jobqueue.tasks import get_pool
        from app.services import runtime_config

        pool = get_pool(app, config)

        # Dispose all instances (release every license) on process exit, then
        # force-kill any orphaned Silver processes as a safety net.
        def _shutdown_pool() -> None:
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

        if pool_enabled and prewarm:
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
    try:
        consumer.run()
    finally:
        stop_reconcile.set()


if __name__ == "__main__":
    main()
