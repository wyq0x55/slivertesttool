"""Durable observational step intervals, separate from machine evidence."""

from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from ...extensions import db

SCHEMA = "ut-platform.job-timing/1"
KEY = "platform_timing"
_STATE_STEPS = {
    "queued": ("queue", "wait"),
    "generating": ("generation", "group"),
    "executing": ("execution_workflow", "group"),
    "repairing": ("repair_workflow", "group"),
    "awaiting_review": ("human_wait", "wait"),
    "needs_human": ("human_wait", "wait"),
    "blocked": ("human_wait", "wait"),
    "error": ("human_wait", "wait"),
    "stopped": ("stopped_wait", "wait"),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _milliseconds(started: str, ended: str) -> float | None:
    try:
        value = (datetime.fromisoformat(ended.replace("Z", "+00:00")) -
                 datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds() * 1000
    except (ValueError, TypeError, AttributeError):
        return None
    return round(value, 3) if value >= 0 else None


def _document(job, *, complete=False) -> dict:
    value = (job.generation or {}).get(KEY)
    if isinstance(value, dict) and value.get("schema") == SCHEMA:
        return copy.deepcopy(value)
    return {"schema": SCHEMA, "history_complete": complete, "steps": []}


def _save(job, document: dict) -> None:
    job.generation = {**(job.generation or {}), KEY: document}


def initialize(job) -> None:
    """Only a newly created job has a complete platform timing history."""
    _save(job, _document(job, complete=True))
    transition(job, "queued")


def transition(job, state: str) -> None:
    """Track workflow/wait intervals without resetting repeated state writes."""
    document, ended = _document(job), now()
    open_state = next((row for row in reversed(document["steps"])
                       if row.get("state") and row["status"] == "running"), None)
    if open_state and open_state["state"] == state:
        return
    _close_driver_wait(document, ended)
    if open_state:
        duration = _milliseconds(open_state["started_at"], ended)
        open_state.update(ended_at=ended, duration_ms=duration, status="completed",
                          duration_source="wall_clock" if duration is not None else "unknown")
    if state in _STATE_STEPS:
        step, kind = _STATE_STEPS[state]
        document["steps"].append({
            "id": len(document["steps"]) + 1, "step": step, "kind": kind,
            "state": state, "status": "running", "started_at": ended,
            "ended_at": None, "duration_ms": None, "duration_source": "wall_clock",
            # Workflow groups span multiple immutable candidate versions.
            "attempt_id": None,
        })
        if state in ("executing", "repairing"):
            document["steps"].append({
                "id": len(document["steps"]) + 1, "step": "driver_wait", "kind": "wait",
                "status": "running", "started_at": ended, "ended_at": None,
                "duration_ms": None, "duration_source": "wall_clock",
            })
    _save(job, document)


def interrupt(job, *, preserve_waits=False) -> None:
    """A restart knows a call died, not when it stopped or how long it ran."""
    document = _document(job)
    for row in document["steps"]:
        preserved = preserve_waits and row.get("step") in ("queue", "driver_wait")
        if row["status"] == "running" and not preserved:
            row.update(status="interrupted", ended_at=None, duration_ms=None, duration_source="unknown")
            document["history_complete"] = False
    _save(job, document)


def _close_driver_wait(document, ended):
    for row in document["steps"]:
        if row["status"] == "running" and row["step"] == "driver_wait":
            duration = _milliseconds(row["started_at"], ended)
            row.update(status="completed", ended_at=ended, duration_ms=duration,
                       duration_source="wall_clock" if duration is not None else "unknown")


def driving(job) -> None:
    """Close the scheduling interval when a worker takes the next step."""
    document = _document(job)
    _close_driver_wait(document, now())
    _save(job, document)


def supersede(job) -> None:
    document, ended = _document(job), now()
    for row in document["steps"]:
        if row["status"] == "running" and row.get("kind") == "wait":
            duration = _milliseconds(row["started_at"], ended)
            row.update(status="completed", ended_at=ended,
                       duration_ms=duration, outcome="superseded",
                       duration_source="wall_clock" if duration is not None else "unknown")
    _save(job, document)


def record_generation(job, progress: dict) -> None:
    """Keep shared observations through progress-file cleanup, outside provenance."""
    document = _document(job)
    project = progress.get("project") or {}
    document["shared_generation"] = {
        "batch_id": job.batch_id, "updated_at": project.get("updated_at"),
        "timings": [copy.deepcopy(row) for row in project.get("timings") or []
                    if row.get("phase") == "extracting"],
    }
    document["function_generation"] = copy.deepcopy(progress.get("function") or {})
    _save(job, document)


@contextmanager
def stage(job, step: str, *, attempt=None, round_number=None, kind="work"):
    document, started = _document(job), now()
    identity = len(document["steps"]) + 1
    document["steps"].append({
        "id": identity, "step": step, "kind": kind, "status": "running",
        "started_at": started, "ended_at": None, "duration_ms": None,
        "duration_source": "monotonic", "attempt_id": attempt.id if attempt else job.current_attempt_id,
        "attempt_seq": attempt.seq if attempt else None,
        "candidate_sha256": attempt.csv_sha256 if attempt else None,
        "round": round_number,
    })
    _save(job, document)
    db.session.commit()
    tick = time.monotonic_ns()
    outcome = {}
    status = "completed"
    try:
        yield outcome
    except BaseException:
        status = "failed"
        raise
    finally:
        elapsed = round((time.monotonic_ns() - tick) / 1_000_000, 3)
        # Read the current metadata so a nested call cannot overwrite its parent
        # or sibling intervals. A rollback may have been needed by a failed call.
        if not db.session.is_active:
            db.session.rollback()
        document = _document(job)
        row = next((item for item in document["steps"] if item["id"] == identity), None)
        if row is not None and row["status"] == "running":
            row.update(ended_at=now(), duration_ms=elapsed, status=status, **outcome)
            _save(job, document)
            db.session.commit()


def snapshot(job, progress: dict) -> dict:
    """GET is read-only; current elapsed values are explicit wall-clock estimates."""
    result = _document(job)
    result["observed_at"] = observed = now()
    for row in result["steps"]:
        row["elapsed_ms"] = (_milliseconds(row["started_at"], observed)
                             if row["status"] == "running" else row.get("duration_ms"))
        row["provisional"] = row["status"] == "running"
    live_function, project = progress.get("function") or {}, progress.get("project") or {}
    saved = job.generation or {}
    shared = result.get("shared_generation") or {}
    stages = copy.deepcopy(saved.get("timings") or [])
    for row in stages:
        row["kind"] = "group" if row.get("phase") == "single_function_generation" else "work"
    phases = copy.deepcopy(saved.get("phase_timings") or result.get("function_generation", {}).get("timings")
                           or live_function.get("timings") or [])
    for row in phases:
        if row.get("phase") == "writing_artifacts":
            row["kind"] = "group"
    function = stages + phases
    result["generation"] = {
        "function": copy.deepcopy(function),
        "shared": copy.deepcopy(shared.get("timings") or [row for row in project.get("timings") or []
                                                          if row.get("phase") == "extracting"]),
        "batch_id": job.batch_id,
        "observed_at": progress.get("observed_at"),
        "function_updated_at": live_function.get("updated_at"),
        "project_updated_at": shared.get("updated_at") or project.get("updated_at"),
    }
    # Completed/superseded/interrupted jobs must not advertise a stale backend
    # phase as still running when its observer never recorded an end event.
    if job.state != "generating" or job.superseded_by:
        for row in result["generation"]["function"] + result["generation"]["shared"]:
            if row.get("status") in ("running", "waiting"):
                row.update(status="interrupted", duration_ms=None)
    return result
