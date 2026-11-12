"""All-in-one launcher: start the web server **and** the task worker together.

    python run.py

This is the recommended way to run the platform: it spawns the Huey worker as a
managed child process and then serves the web app in the foreground, so a single
command brings up everything and the worker "always runs" for as long as the
server is up. Press Ctrl+C to stop both cleanly.

Why two processes at all? Huey's model separates *enqueue* (done by the web app
when you submit a task) from *execute* (done by the worker). Keeping execution in
a separate process means a long-running Silver test never blocks the web server
or the live SSE log streams. This launcher just manages both for you.

For scaled / service deployments you can still run them independently:

    python run_web.py        # web only
    python run_worker.py     # one or more workers, possibly on other hosts

Set ``START_WORKER=0`` to have this launcher start the web server only.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import time
from pathlib import Path

from app.config import Config
from app.logging_setup import configure as configure_logging
from app.runners import process_guard

# The launcher serves the web app in the foreground, so it takes the "web" role;
# the worker and collab children configure their own files in their own
# processes (never share one rotating handle across processes).
configure_logging("web", Config)

from app import __version__, create_app  # noqa: E402  (after logging setup)
from app.bootstrap import bootstrap_app  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent

# Build the Flask app without mutating persistent state. bootstrap_app() runs
# once in main() before any child process is spawned.
app = create_app()

_worker_proc: subprocess.Popen | None = None
_collab_proc: subprocess.Popen | None = None
# Kill-on-close job holding the worker (and, through inheritance, every Silver
# process it starts). Kept referenced for the launcher's lifetime: if the
# launcher itself is killed, the kernel closes the handle and the worker tree
# goes with it instead of lingering with licenses held.
_worker_job: process_guard.KillOnCloseJob | None = None

# How long the worker gets to dispose its Silver instances after a graceful
# stop request before it is killed. Disposing is sequential per instance.
_WORKER_STOP_TIMEOUT_SECONDS = 60


def _start_worker() -> subprocess.Popen | None:
    if os.environ.get("START_WORKER", "1").strip().lower() in ("0", "false", "no", "off"):
        print("START_WORKER=0 -> not starting the worker (web only).")
        return None
    global _worker_job
    print("Starting task worker (run_worker.py)...")
    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "run_worker.py")],
        cwd=str(BASE_DIR),
        env=os.environ.copy(),
        # Own process group, so request_stop() can deliver CTRL_BREAK_EVENT.
        creationflags=process_guard.child_creationflags(),
    )
    if _worker_job is None:
        _worker_job = process_guard.KillOnCloseJob.create()
    if _worker_job is not None:
        _worker_job.assign_pid(proc.pid)
    return proc


def _start_collab() -> subprocess.Popen | None:
    """Spawn the real-time collaboration ASGI server (run_collab.py).

    Waitress cannot do WebSocket upgrades, so collaboration runs as its own
    uvicorn process. This launcher manages it just like the worker, so a single
    ``python run.py`` brings up web + worker + collab together.

    Set ``START_COLLAB=0`` to skip it (e.g. a deployment without the collab
    dependencies installed). If ``run_collab.py`` exits immediately because its
    optional dependencies are missing, the web app keeps running unaffected and
    the editor stays on its classic REST path.
    """
    if os.environ.get("START_COLLAB", "1").strip().lower() in ("0", "false", "no", "off"):
        print("START_COLLAB=0 -> not starting the collaboration server.")
        return None
    print("Starting collaboration server (run_collab.py)...")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "run_collab.py")],
            cwd=str(BASE_DIR),
            env=os.environ.copy(),
        )
    except Exception as exc:  # noqa: BLE001 - never let collab startup crash launch
        print(f"Could not start collaboration server: {exc} (continuing web-only).")
        return None
    return proc


def _begin_silver_drain() -> None:
    """Stop new Silver work from starting while the worker shuts down.

    The drain flag is a transient shutdown state; the configured license limit
    is never changed. Disposing the instances is the worker's own job, done
    from its exit path once :func:`_stop_worker` asks it to stop.
    """
    if _worker_proc is None or _worker_proc.poll() is not None:
        return
    if (getattr(Config, "RUNNER_BACKEND", "mock") or "").lower() != "silver":
        return
    try:
        from app.services import license_service

        with app.app_context():
            license_service.begin_drain()
    except Exception:  # noqa: BLE001 - shutdown must never raise
        pass


def _stop_worker() -> None:
    global _worker_proc
    if _worker_proc is None:
        return
    if _worker_proc.poll() is None:
        print("\nStopping task worker...")
        _begin_silver_drain()
        # A graceful request lets the worker run its exit path, which disposes
        # every Silver instance and releases its license. On Windows a plain
        # terminate() is TerminateProcess and would skip that entirely.
        if process_guard.request_stop(_worker_proc):
            timeout = _WORKER_STOP_TIMEOUT_SECONDS
        else:
            _worker_proc.terminate()
            timeout = 10
        try:
            _worker_proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print("Task worker did not stop in time; killing it.")
            # The worker's own kill-on-close job then takes its Silver children.
            _worker_proc.kill()
            try:
                _worker_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    _worker_proc = None
    _sweep_silver()


def _stop_collab() -> None:
    global _collab_proc
    if _collab_proc is None:
        return
    if _collab_proc.poll() is None:
        print("\nStopping collaboration server...")
        _collab_proc.terminate()
        try:
            _collab_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _collab_proc.kill()
    _collab_proc = None


def _stop_children() -> None:
    _stop_collab()
    _stop_worker()


def _sweep_silver() -> None:
    """Force-kill any leftover Silver processes after the worker stops.

    This is an explicit emergency fallback for a dedicated Silver host. It is
    disabled by default because image-name sweeping cannot distinguish this
    application's Silver processes from unrelated/manual Silver sessions.
    """
    try:
        if (getattr(Config, "RUNNER_BACKEND", "mock") or "").lower() != "silver":
            return
        if not getattr(Config, "SILVER_KILL_ON_EXIT", False):
            return
        from app.runners.silver_cleanup import force_kill_silver_processes
        force_kill_silver_processes(getattr(Config, "SILVER_PROCESS_IMAGE_NAMES", None))
    except Exception:  # noqa: BLE001 - cleanup must never raise on shutdown
        pass


def main() -> None:
    global _worker_proc, _collab_proc
    bootstrap_app(app)
    _worker_proc = _start_worker()
    _collab_proc = _start_collab()
    atexit.register(_stop_children)
    # Give the worker a moment to attach to the queue before serving.
    time.sleep(0.5)

    print(f"Test Platform v{__version__} -> http://{Config.HOST}:{Config.PORT}")
    try:
        from waitress import serve

        serve(app, host=Config.HOST, port=Config.PORT,
              threads=max(16, Config.HUEY_WORKERS * 2))
    except ImportError:  # pragma: no cover - dev fallback
        print("waitress not installed; using Flask's dev server.")
        app.run(host=Config.HOST, port=Config.PORT, threaded=True)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        _stop_children()


if __name__ == "__main__":
    main()
