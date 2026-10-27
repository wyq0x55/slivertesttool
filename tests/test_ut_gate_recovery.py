"""Gate codes, the model-stall breaker and start-up recovery of interrupted jobs.

Reuses the scripted bridge of test_ut_pipeline (Huey in immediate mode).
"""

from __future__ import annotations

from test_ut_pipeline import (
    GAP,
    _configure,
    _enqueue,
    _job,
    _llm_reply,
    _row,
)
from test_ut_pipeline import env as workflow_env

from app.services.ai import provider
from app.services.ut_pipeline import pipeline

env = workflow_env


def _replies(monkeypatch, *replies):
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(messages)
        return replies[min(len(calls), len(replies)) - 1]

    monkeypatch.setattr(provider, "chat", fake_chat)
    return calls


def test_a_model_that_proposes_nothing_stops_after_one_round(env, monkeypatch):
    targets = _configure(env)
    env.bridge.gaps = [GAP]
    calls = _replies(monkeypatch, _llm_reply())
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert job["state"] == "needs_human" and job["gate_code"] == "MODEL_STALLED"
    assert len(calls) == 1 and job["repair_rounds"] == 1
    assert "没有提出任何新增行" in job["reason"] and "fix" in job["reason"]


def test_rejected_rows_are_feedback_not_a_stall(env, monkeypatch):
    targets = _configure(env, rounds=2)
    env.bridge.gaps = [GAP]
    # Names an obligation that is not open: the row is rejected, and the next
    # round sees that rejection, so it is worth asking again.
    calls = _replies(monkeypatch, _llm_reply(_row("b9:NOT_OPEN")))
    [job_id] = _enqueue(env, targets["f"])
    job = _job(env, job_id)
    assert len(calls) == 2
    assert job["state"] == "needs_human" and job["gate_code"] == "OPEN_GAPS_EXHAUSTED"


def test_every_gate_carries_a_code_and_a_decision_clears_it(env, monkeypatch):
    targets = _configure(env)
    [passed] = _enqueue(env, targets["f"])
    assert _job(env, passed)["gate_code"] == "MACHINE_PASS"
    status, body = env.call("post", f"/jobs/{passed}/review", json={"decision": "approve"})
    assert status == 200 and body["data"]["gate_code"] is None

    env.bridge.execution_status = "BLOCKED"
    [blocked] = _enqueue(env, targets["g"])
    assert _job(env, blocked)["gate_code"] == "ENV_BLOCKED"

    env.bridge.execution_status = "EXECUTED"
    env.bridge.gaps = [GAP]

    def offline(*a, **k):
        raise provider.ProviderError("offline")

    monkeypatch.setattr(provider, "chat", offline)
    [unavailable] = _enqueue(env, targets["f"])
    job = _job(env, unavailable)
    assert (job["state"], job["gate_code"], job["repair_rounds"]) == ("error", "MODEL_UNAVAILABLE", 0)

    env.bridge.generated = None
    [no_csv] = _enqueue(env, targets["g"])
    assert _job(env, no_csv)["gate_code"] == "NO_TESTCSV"


def test_startup_parks_jobs_the_dead_worker_owned(env, app_ctx, monkeypatch):
    from app.extensions import db
    from app.models import AuditLog, LMUser, UtBatch, UtJob

    targets = _configure(env)
    [executing] = _enqueue(env, targets["f"])   # has a candidate
    [stopping] = _enqueue(env, targets["g"])
    with app_ctx.app_context():
        admin = LMUser.query.filter_by(is_system_admin=True).first()
        generating_batch = pipeline.create_batch(env.pid, [targets["f"]], admin.id).id
        pending_batch = pipeline.create_batch(env.pid, [targets["g"]], admin.id).id
        generating = UtJob.query.filter_by(batch_id=generating_batch).one()
        generating.state = UtJob.GENERATING
        db.session.get(UtJob, executing).state = UtJob.EXECUTING
        job = db.session.get(UtJob, stopping)
        job.state, job.stop_requested_at = UtJob.REPAIRING, job.updated_at
        db.session.commit()
        generating_id = generating.id

        result = pipeline.recover_interrupted(set(), {pending_batch})
        assert sorted(result["interrupted"]) == sorted([executing, generating_id])
        assert result["stopped"] == [stopping]
        assert len(result["pending"]) == 1  # queued behind a generate task still in Huey
        assert db.session.get(UtBatch, generating_batch).status == "failed"
        assert AuditLog.query.filter_by(action="ut.job.interrupted").count() == 3

    job = _job(env, executing)
    assert (job["state"], job["gate_code"]) == ("error", "INTERRUPTED")
    assert "点「重试」继续" in job["reason"]
    assert _job(env, generating_id)["reason"].endswith("请在「任务/运行」重新入队")
    assert _job(env, stopping)["state"] == "stopped"


def test_an_interrupted_job_resumes_from_its_kept_candidate(env, app_ctx):
    from app.extensions import db
    from app.models import UtJob

    targets = _configure(env)
    [job_id] = _enqueue(env, targets["f"])
    with app_ctx.app_context():
        db.session.get(UtJob, job_id).state = UtJob.EXECUTING
        db.session.commit()
        assert pipeline.recover_interrupted(set(), set())["interrupted"] == [job_id]
    status, _ = env.call("post", f"/jobs/{job_id}/retry")
    job = _job(env, job_id)
    assert status == 202 and job["state"] == "awaiting_review" and job["gate_code"] == "MACHINE_PASS"


def test_the_worker_hook_skips_jobs_with_a_queued_task(env, app_ctx, monkeypatch):
    from app.jobqueue import ut_tasks

    targets = _configure(env)
    seen = {}
    monkeypatch.setattr(pipeline, "recover_interrupted",
                        lambda jobs, batches: seen.update(jobs=jobs, batches=batches) or {})

    monkeypatch.setattr(ut_tasks.huey, "pending", lambda limit: [
        ut_tasks.ut_advance_job.s(7), ut_tasks.ut_generate_batch.s(3)])
    ut_tasks.recover_interrupted_jobs()
    assert seen == {"jobs": {7}, "batches": {3}}
    assert targets  # binding configured; nothing else to drive
