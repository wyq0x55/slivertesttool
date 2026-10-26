"""PostgreSQL lifecycle checks for per-function generation handoff."""

import hashlib
from pathlib import Path

import pytest
from test_ut_pipeline import _configure, _enqueue, _job
from test_ut_pipeline import env as workflow_env

from app.services.ut_pipeline import pipeline
from app.services.ut_pipeline.bridge import BridgeError

env = workflow_env


def _record(unit, provenance):
    artifacts = {}
    if unit.get("testcsv"):
        path = Path(unit["testcsv"])
        artifacts["testcsv"] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"contract": "ut-agent.generated-unit/1", "provenance": provenance, "unit": unit,
            "artifacts": artifacts}


def _stream(env, monkeypatch, between, *, duplicate=False, late_error=False, conflict=False, provenance_conflict=False):
    generate = env.bridge.generate

    def emit(manifest, index, product, out, functions, *, on_unit, **kwargs):
        first = generate(manifest, index, product, out, functions[:1], **kwargs)
        record = _record(first["units"][0], first["provenance"])
        on_unit(record)
        if duplicate:
            on_unit(record)
        between(first)
        if late_error:
            raise BridgeError("late sibling failed")
        second = generate(manifest, index, product, out, functions[1:], **kwargs)
        if conflict:
            first["units"][0] = {**first["units"][0], "status": "FAILED"}
        if provenance_conflict:
            first["provenance"] = {"different": "generator"}
        return {**first, "units": first["units"] + second["units"]}

    monkeypatch.setattr(env.bridge, "generate", emit)


def test_ready_function_executes_before_sibling_finishes_and_is_not_dispatched_twice(env, monkeypatch):
    from app.models import UtAttempt, UtBatch, UtJob

    targets = _configure(env)
    seen = []

    def between(document):
        first = UtJob.query.join(UtJob.target).filter_by(function="f").one()
        sibling = UtJob.query.join(UtJob.target).filter_by(function="g").one()
        batch = UtBatch.query.one()
        assert first.state == "awaiting_review" and sibling.state == "generating"
        assert batch.status == "generating" and batch.constraints
        assert batch.provenance == document["provenance"]
        assert UtAttempt.query.filter_by(job_id=first.id).count() == 1 and env.bridge.executions == 1
        seen.append(first.id)

    _stream(env, monkeypatch, between, duplicate=True)
    ids = _enqueue(env, targets["f"], targets["g"])
    assert len(seen) == 1 and env.bridge.executions == 2
    assert all(_job(env, job_id)["state"] == "awaiting_review" for job_id in ids)
    assert all(len(_job(env, job_id)["attempts"]) == 1 for job_id in ids)
    assert _job(env, ids[0])["batch"]["status"] == "done"


@pytest.mark.parametrize("conflict", ["bridge", "unit", "provenance"])
def test_late_generation_failure_preserves_the_completed_function(env, monkeypatch, conflict):
    targets = _configure(env)
    _stream(env, monkeypatch, lambda _: None, late_error=conflict == "bridge", conflict=conflict == "unit",
            provenance_conflict=conflict == "provenance")
    ids = _enqueue(env, targets["f"], targets["g"])
    jobs = {_job(env, job_id)["target"]["function"]: _job(env, job_id) for job_id in ids}
    assert jobs["f"]["state"] == "awaiting_review" and len(jobs["f"]["attempts"]) == 1
    assert jobs["f"]["generation_status"] == "VALIDATED"
    assert jobs["g"]["state"] == "error" and jobs["g"]["attempts"] == []
    assert jobs["g"]["batch"]["status"] == "failed" and env.bridge.executions == 1


def test_stop_requested_while_generating_is_refreshed_at_handoff(env, monkeypatch):
    from app.models import LMUser, UtJob

    targets = _configure(env)
    generate = env.bridge.generate

    def stopped(*args, on_unit, **kwargs):
        document = generate(*args, **kwargs)
        job = UtJob.query.join(UtJob.target).filter_by(function="f").one()
        admin = LMUser.query.filter_by(is_system_admin=True).first()
        assert pipeline.stop_job(env.pid, job.id, admin.id).stop_requested_at
        for unit in document["units"]:
            on_unit(_record(unit, document["provenance"]))
        return document

    monkeypatch.setattr(env.bridge, "generate", stopped)
    ids = _enqueue(env, targets["f"], targets["g"])
    states = {_job(env, job_id)["target"]["function"]: _job(env, job_id) for job_id in ids}
    assert states["f"]["state"] == "stopped" and len(states["f"]["attempts"]) == 1
    assert states["g"]["state"] == "awaiting_review" and env.bridge.executions == 1


def test_occupied_generation_slot_reschedules_without_holding_another_worker(env, monkeypatch):
    from app.jobqueue import ut_tasks

    targets = _configure(env)
    scheduled = []
    monkeypatch.setattr(ut_tasks.ut_generate_batch, "schedule", lambda **kwargs: scheduled.append(kwargs))
    assert pipeline._GENERATION_GATE.acquire(blocking=False)
    try:
        ids = _enqueue(env, targets["f"])
    finally:
        pipeline._GENERATION_GATE.release()
    assert len(scheduled) == 1 and scheduled[0]["delay"] == 2
    assert _job(env, ids[0])["state"] == "queued" and env.bridge.executions == 0


def test_delayed_generation_task_is_retained_during_worker_recovery(env, monkeypatch):
    from app.jobqueue import ut_tasks

    targets = _configure(env)
    monkeypatch.setattr(ut_tasks.ut_generate_batch, "schedule", lambda **_: None)
    assert pipeline._GENERATION_GATE.acquire(blocking=False)
    try:
        [job_id] = _enqueue(env, targets["f"])
    finally:
        pipeline._GENERATION_GATE.release()
    batch_id = _job(env, job_id)["batch_id"]
    monkeypatch.setattr(ut_tasks.huey, "pending", lambda **_: [])
    monkeypatch.setattr(ut_tasks.huey, "scheduled", lambda **_: [ut_tasks.ut_generate_batch.s(batch_id)])
    result = ut_tasks.recover_interrupted_jobs()
    assert result["pending"] == [job_id] and result["interrupted"] == []
    assert _job(env, job_id)["state"] == "queued"


def test_changed_csv_between_receipt_validation_and_handoff_is_not_accepted(env, monkeypatch):
    targets = _configure(env)
    generate = env.bridge.generate

    def changed(*args, on_unit, **kwargs):
        document = generate(*args, **kwargs)
        unit = document["units"][0]
        record = _record(unit, document["provenance"])
        Path(unit["testcsv"]).write_bytes(b"changed after verification")
        on_unit(record)
        return document

    monkeypatch.setattr(env.bridge, "generate", changed)
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "error" and job["attempts"] == [] and env.bridge.executions == 0
    assert "changed after receipt" in job["reason"]


def test_stopped_unsupported_unit_stays_stopped_without_a_review_gate(env, monkeypatch):
    from app.models import LMUser, UtJob

    targets = _configure(env)
    env.bridge.generated = None
    generate = env.bridge.generate

    def stopped(*args, on_unit, **kwargs):
        document = generate(*args, **kwargs)
        job = UtJob.query.one()
        admin = LMUser.query.filter_by(is_system_admin=True).first()
        pipeline.stop_job(env.pid, job.id, admin.id)
        on_unit(_record(document["units"][0], document["provenance"]))
        return document

    monkeypatch.setattr(env.bridge, "generate", stopped)
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "stopped" and job["gate_code"] == "STOPPED"
    assert job["attempts"] == [] and env.bridge.executions == 0


def test_requeued_completed_function_is_not_overwritten_by_the_old_batch(env, monkeypatch):
    from app.jobqueue import ut_tasks
    from app.models import UtJob

    targets = _configure(env)
    monkeypatch.setattr(ut_tasks.ut_generate_batch, "schedule", lambda **_: None)
    replacements = []

    def between(_):
        previous = UtJob.query.join(UtJob.target).filter_by(function="f").one()
        [replacement] = _enqueue(env, targets["f"])
        replacements.append((previous.id, replacement))

    _stream(env, monkeypatch, between)
    ids = _enqueue(env, targets["f"], targets["g"])
    old_id, new_id = replacements[0]
    assert old_id in ids
    old, new = _job(env, old_id), _job(env, new_id)
    assert old["state"] == "awaiting_review" and old["superseded_by"] == new_id
    assert len(old["attempts"]) == 1 and new["state"] == "queued" and new["attempts"] == []


def test_delayed_duplicate_does_not_hide_interrupted_generation_during_recovery(env, monkeypatch):
    from app.extensions import db
    from app.jobqueue import ut_tasks
    from app.models import UtJob

    targets = _configure(env)
    monkeypatch.setattr(ut_tasks.ut_generate_batch, "schedule", lambda **_: None)
    assert pipeline._GENERATION_GATE.acquire(blocking=False)
    try:
        [job_id] = _enqueue(env, targets["f"])
    finally:
        pipeline._GENERATION_GATE.release()
    with env.client.application.app_context():
        job = db.session.get(UtJob, job_id)
        job.state = UtJob.GENERATING
        batch_id = job.batch_id
        db.session.commit()
    monkeypatch.setattr(ut_tasks.huey, "pending", lambda **_: [])
    monkeypatch.setattr(ut_tasks.huey, "scheduled", lambda **_: [ut_tasks.ut_generate_batch.s(batch_id)])
    assert ut_tasks.recover_interrupted_jobs()["interrupted"] == [job_id]
    ut_tasks.ut_generate_batch(batch_id)
    job = _job(env, job_id)
    assert job["state"] == "error" and job["gate_code"] == "INTERRUPTED"
    assert job["batch"]["status"] == "failed" and env.bridge.executions == 0


def test_duplicate_generation_task_cannot_finish_active_execution_or_unfinished_generation(env, monkeypatch):
    from app.extensions import db
    from app.jobqueue import ut_tasks
    from app.models import UtAttempt, UtJob

    targets = _configure(env)
    monkeypatch.setattr(ut_tasks.ut_generate_batch, "schedule", lambda **_: None)
    assert pipeline._GENERATION_GATE.acquire(blocking=False)
    try:
        [job_id] = _enqueue(env, targets["f"])
    finally:
        pipeline._GENERATION_GATE.release()
    with env.client.application.app_context():
        job = db.session.get(UtJob, job_id)
        batch_id = job.batch_id
        attempt = pipeline._new_attempt(job, UtAttempt.ORIGIN_GENERATOR, env.bridge.generated)
        job.reference_csv, job.state = attempt.csv_path, UtJob.EXECUTING
        db.session.commit()
        assert pipeline.run_generation(batch_id) == []
    assert _job(env, job_id)["batch"]["status"] == "running"

    # Exercise the defensive path even if startup recovery was not called.
    from app.models import UtBatch

    with env.client.application.app_context():
        job = db.session.get(UtJob, job_id, populate_existing=True)
        job.state = UtJob.GENERATING
        db.session.get(UtBatch, batch_id).status = UtBatch.STATUS_GENERATING
        db.session.commit()
        assert pipeline.run_generation(batch_id) == []
    job = _job(env, job_id)
    assert job["state"] == "error" and job["batch"]["status"] == "failed"
