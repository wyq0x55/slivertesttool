"""Bridge between the Task/event world and the Silver execution backends.

Runs inside the Huey worker process. Responsibilities:

* assemble the run directory / log directory from the task workspace,
* stream live log lines (by tailing ``Console.log``) and coarse progress into
  ``task_events`` so the browser sees realtime updates over SSE,
* honour cooperative cancellation (a monitor thread watches the task's
  ``cancel_requested`` flag in the DB and force-stops the live Silver handle),
* parse the judge verdict, package the artefacts into a downloadable report,
  and record the final task status.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from ..extensions import db
from ..models import Task, TaskStatus
from ..services import event_service, report_service
from . import run_layout
from .silver_runner import (
    RunContext,
    RunnerCancelled,
    RunnerError,
    build_runner,
)

logger = logging.getLogger("silvetestapp.runner")

_MONITOR_POLL_SECONDS = 0.5
_TAIL_POLL_SECONDS = 0.4


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# jdgrslt.log markers produced by the TestCaseCreator judge. Every line is
# prefixed with a logging tag (e.g. "INFO:root:"). The judge writes, per step:
#   "Step.3 is failed at 5.001s."   /   "Step.1 is passed at 0.0s."
# sub-routine steps as "<Subroutine> Step.2 is passed at ..." and, per test
# case, an overall line:
#   "All steps are verified.Test is Passed."   /   "Test is failed in Step3!!!!"
# One jdgrslt.log can contain SEVERAL test cases, each opened by:
#   "Test case ID.<test_id> is started!"
_CASE_START_RE = re.compile(r"Test case ID\.(.+?)\s+is started!", re.IGNORECASE)
_STEP_FAIL_RE = re.compile(r"Step\.\d+\s+is\s+failed", re.IGNORECASE)
_STEP_PASS_RE = re.compile(r"Step\.\d+\s+is\s+passed", re.IGNORECASE)
_TEST_FAIL_RE = re.compile(r"Test\s+is\s+failed", re.IGNORECASE)
_TEST_PASS_RE = re.compile(
    r"Test\s+is\s+Passed|All\s+steps\s+are\s+verified", re.IGNORECASE
)


def extract_case_section(text: str, test_id: str) -> str:
    """Return only the log section for ``test_id``.

    A jdgrslt.log may accumulate several test cases; scoping to the requested
    one prevents a passing test from inheriting another case's failure. If no
    per-case markers exist (or the id is not found), the full text is returned.
    """
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if _CASE_START_RE.search(ln)]
    if not starts:
        return text
    tid = (test_id or "").strip()
    for idx, start in enumerate(starts):
        m = _CASE_START_RE.search(lines[start])
        marker_id = (m.group(1).strip() if m else "")
        if tid and (marker_id == tid or tid in lines[start] or marker_id in tid):
            end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
            return "\n".join(lines[start:end])
    # Fall back to the last case if the id could not be matched exactly.
    return "\n".join(lines[starts[-1]:])


def line_failed(line: str) -> bool:
    """True if a line records a failing step or an overall test failure."""
    return bool(_STEP_FAIL_RE.search(line) or _TEST_FAIL_RE.search(line))


def line_passed(line: str) -> bool:
    """True if a line records a passing step or an overall pass."""
    return bool(_STEP_PASS_RE.search(line) or _TEST_PASS_RE.search(line))


def count_failed_steps(text: str, test_id: str = "") -> int:
    """Count failing *step* lines (``Step.N is failed``) for the test case."""
    section = extract_case_section(text, test_id) if test_id else text
    return sum(1 for ln in section.splitlines() if _STEP_FAIL_RE.search(ln))


def parse_verdict_text(text: str, test_id: str = "") -> str:
    """Derive the judge verdict for ``test_id`` from a ``jdgrslt.log`` body.

    Order of precedence within the test case's own section:
      1. An overall ``Test is Passed`` / ``Test is failed`` marker wins.
      2. Otherwise, any failing ``Step.N is failed`` line -> ``FAIL``.
      3. Otherwise a passing step marker -> ``PASS``.
      4. Otherwise ``UNKNOWN``.
    """
    if not text.strip():
        return "UNKNOWN"
    section = extract_case_section(text, test_id) if test_id else text

    # 1. Overall test markers are authoritative.
    if _TEST_FAIL_RE.search(section):
        return "FAIL"
    if _TEST_PASS_RE.search(section):
        return "PASS"

    # 2. Any failing step.
    if any(_STEP_FAIL_RE.search(ln) for ln in section.splitlines()):
        return "FAIL"

    # 3. A passing step with no failures.
    if any(_STEP_PASS_RE.search(ln) for ln in section.splitlines()):
        return "PASS"
    return "UNKNOWN"


def _parse_verdict(log_dir: Path, test_id: str = "") -> str:
    """Read + parse the judge verdict from ``jdgrslt.log`` in ``log_dir``."""
    jdg = log_dir / "jdgrslt.log"
    if not jdg.is_file():
        return "UNKNOWN"
    try:
        text = jdg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "UNKNOWN"
    return parse_verdict_text(text, test_id)


def _monitor_cancel(app, task_pk: int, cancel_event: threading.Event,
                    handle_box: dict, stop: threading.Event,
                    force_stop=None) -> None:
    """Watch the DB cancel flag; trip the event + force-stop Silver.

    ``force_stop`` is the callable that terminates the live run given the Silver
    handle. For a dedicated instance this exits the process; for a pooled
    instance it force-stops the current run (the instance is then poisoned by
    the caller and replaced by the pool).
    """
    if force_stop is None:
        from .silver_runner import SilverRunner
        force_stop = SilverRunner.force_stop

    while not stop.wait(_MONITOR_POLL_SECONDS):
        try:
            with app.app_context():
                task = db.session.get(Task, task_pk)
                requested = bool(task and task.cancel_requested)
        except Exception:  # noqa: BLE001
            continue
        if requested and not cancel_event.is_set():
            cancel_event.set()
            handle = handle_box.get("handle")
            if handle is not None:
                try:
                    force_stop(handle)
                except Exception:  # noqa: BLE001 - best effort
                    pass
            return


def _tail_console(app, task_pk: int, console_path: Path,
                  stop: threading.Event, start_offset: int = 0) -> None:
    """Stream new lines from Console.log into task_events as log events.

    ``start_offset`` lets a *reused* pool instance -- whose console log
    accumulates across runs -- be tailed from the byte offset where this run
    began, so only this run's lines are streamed.
    """
    pos = start_offset
    while not stop.is_set():
        try:
            if console_path.is_file():
                with console_path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                if chunk:
                    with app.app_context():
                        task = db.session.get(Task, task_pk)
                        if task is not None:
                            for line in chunk.splitlines():
                                if line.strip():
                                    event_service.emit_log(task, line.rstrip())
        except Exception:  # noqa: BLE001 - tailing must never crash the run
            pass
        time.sleep(_TAIL_POLL_SECONDS)


def execute(app, config, task: Task, pool=None, instance=None) -> None:
    """Execute one task end-to-end. Must be called within an app context.

    When *pool* and *instance* are supplied the job runs on a pre-warmed,
    reusable pool instance (fast path -- no Silver process launch). Otherwise a
    dedicated instance is launched for this job (classic path). Both paths share
    identical configuration, cancellation and reporting logic.
    """
    pooled = pool is not None and instance is not None

    # ``task.workspace`` is the persistent per-project root; results are keyed by
    # test id. Run scripts were materialised into a staging dir at enqueue time
    # and are now copied into the chosen runtime instance dir (deleted once the
    # run finishes).
    workspace = Path(task.workspace)
    log_dir = run_layout.log_dir(workspace, task.test_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    staging = run_layout.staging_dir(workspace, task.test_id)
    run_dir = run_layout.instance_run_dir(
        config, task.id, task.test_id, instance if pooled else None)
    shutil.rmtree(run_dir, ignore_errors=True)
    if staging.is_dir():
        shutil.copytree(staging, run_dir)
    else:
        run_dir.mkdir(parents=True, exist_ok=True)
    # ``sil_relpath`` holds either an absolute server-side model path (the
    # admin-registered model registry) or a path relative to the run directory
    # (the legacy in-bundle flow).
    sil_ref = Path(task.sil_relpath)
    sil_path = sil_ref if sil_ref.is_absolute() else (run_dir / sil_ref).resolve()

    cancel_event = threading.Event()
    handle_box: dict = {"handle": None}
    stop_threads = threading.Event()

    # A reused instance writes its console to a stable per-instance file that
    # grows across runs; tail (and later slice) it from the current end so this
    # run only sees its own output. A dedicated instance logs into log_dir.
    console_path = log_dir / "Console.log"
    start_offset = 0
    if pooled and getattr(instance, "console_log", None) is not None:
        console_path = Path(instance.console_log)
        try:
            start_offset = console_path.stat().st_size if console_path.is_file() else 0
        except OSError:
            start_offset = 0

    force_stop = (lambda h: pool.force_stop(instance)) if pooled else None

    monitor = threading.Thread(
        target=_monitor_cancel,
        args=(app, task.id, cancel_event, handle_box, stop_threads, force_stop),
        daemon=True,
    )
    tailer = threading.Thread(
        target=_tail_console,
        args=(app, task.id, console_path, stop_threads, start_offset),
        daemon=True,
    )
    monitor.start()
    tailer.start()

    def _on_start(handle: Any) -> None:
        handle_box["handle"] = handle

    ctx = RunContext(
        test_id=task.test_id,
        run_dir=run_dir,
        log_dir=log_dir,
        sil_path=sil_path,
        gui=config.SILVER_GUI,
        timeout=config.EXECUTION_TIMEOUT,
        cancel_event=cancel_event,
        on_start=_on_start,
        reload_model=pooled,
        console_log=console_path,
    )

    if pooled:
        event_service.emit_log(
            task, f"Reusing pre-warmed Silver instance for test '{task.test_id}'...")
    else:
        event_service.emit_log(
            task, f"Launching Silver for test '{task.test_id}'...")
    event_service.emit_progress(task, 10)

    try:
        if pooled:
            # Publish the handle up-front so a cancel can force-stop the run.
            _on_start(instance.handle)
            pool.configure_and_run(instance, ctx)
        else:
            runner = build_runner(config.RUNNER_BACKEND)
            runner.run(ctx)
    except RunnerCancelled:
        stop_threads.set()
        if pooled and not pool.is_mock:
            # The instance's process was force-killed to unblock the run; drop
            # it so the pool recreates a clean replacement (re-grabbing the
            # license) on the next demand.
            pool.poison(instance)
        _finalise(task, TaskStatus.CANCELLED, "Cancelled by user.", verdict="CANCELLED")
        event_service.emit_result(task, "cancelled", "Cancelled by user.")
        return
    except RunnerError as exc:
        stop_threads.set()
        if pooled and not pool.is_mock:
            pool.poison(instance)
        _finalise(task, TaskStatus.FAILED, str(exc), verdict="ERROR")
        event_service.emit_error(task, str(exc))
        event_service.emit_result(task, "failed", str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        stop_threads.set()
        if pooled and not pool.is_mock:
            pool.poison(instance)
        logger.exception("Unexpected failure in task %s", task.task_key)
        _finalise(task, TaskStatus.FAILED, f"Internal error: {exc}", verdict="ERROR")
        event_service.emit_error(task, f"Internal error: {exc}")
        event_service.emit_result(task, "failed", f"Internal error: {exc}")
        return
    finally:
        stop_threads.set()
        # Run scripts live only for the duration of the run: drop the runtime
        # instance copy and the enqueue-time staging dir (results stay in
        # ``log_dir``). Runs on every exit path (success, error, cancel).
        shutil.rmtree(run_dir, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)

    # For a reused instance, copy this run's slice of the shared console log into
    # the task's own Console.log so the packaged report is self-contained.
    if pooled and console_path != (log_dir / "Console.log"):
        _slice_console(console_path, start_offset, log_dir / "Console.log")

    # Give the tailer a beat to flush the final Console.log lines.
    time.sleep(_TAIL_POLL_SECONDS)
    event_service.emit_progress(task, 90)

    verdict = _parse_verdict(log_dir, task.test_id)
    # Results are compressed on demand at download time straight from ``log_dir``
    # (see report_service.build_report_stream), so no report.zip snapshot is
    # stored here. Record the results dir as the download source.
    task.report_path = str(log_dir)
    db.session.add(task)
    db.session.commit()

    passed = verdict.upper().startswith("PASS")
    status = TaskStatus.PASSED if passed else TaskStatus.FAILED
    _finalise(task, status, f"Execution finished. Verdict: {verdict}.", verdict=verdict)
    event_service.emit_progress(task, 100)
    event_service.emit_result(task, "pass" if passed else "fail",
                              f"Verdict: {verdict}")


def _slice_console(source: Path, start_offset: int, dest: Path) -> None:
    """Copy ``source[start_offset:]`` into ``dest`` (best effort)."""
    try:
        if not source.is_file():
            dest.write_text("", encoding="utf-8")
            return
        with source.open("rb") as fh:
            fh.seek(start_offset)
            data = fh.read()
        dest.write_bytes(data)
    except OSError:
        logger.warning("Could not slice console log %s -> %s", source, dest)


def _finalise(task: Task, status: TaskStatus, message: str, verdict: str) -> None:
    task.status = status.value
    task.message = message
    task.result = verdict
    task.finished_at = _utcnow()
    db.session.add(task)
    db.session.commit()
